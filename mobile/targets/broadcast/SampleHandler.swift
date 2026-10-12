import CoreMedia
import CoreVideo
import Foundation
import ReplayKit
import os

/// The broadcast upload extension: the only process on iOS that can hear
/// another app's audio. Runs while the user has the broadcast on (red pill in
/// the status bar), under a 50 MB memory limit.
///
/// Everything with logic lives in targets/_shared (Linux-tested). This file
/// only converts ReplayKit buffers into plain values and wires up the
/// cross-process signals:
///   app intent --(Darwin notification "check")--> here --> /v2/check
///   here --(last-result.json + Darwin "result")--> app intent shows the verdict
final class SampleHandler: RPBroadcastSampleHandler {
    private let queue = DispatchQueue(label: "asterisk.capture")
    private var pipeline: CapturePipeline?
    private var transport: HTTPTransport?
    private var config = CaptureConfig()
    private let resampler = Resampler()
    private var diag: DiagLog?
    private var timer: DispatchSourceTimer?
    private var lastFrameMs: Int64 = 0
    private var startedMs: Int64 = 0

    // Counters for the diagnostics heartbeat.
    private var audioBuffers = 0
    private var audioSamplesOut = 0
    private var videoFrames = 0
    private var framesAnalysed = 0
    private var loggedFormat = false
    private var loggedPixelFormat = false

    private var container: URL? {
        FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: Shared.appGroup)
    }

    // MARK: lifecycle

    override func broadcastStarted(withSetupInfo setupInfo: [String: NSObject]?) {
        startedMs = DiagLog.nowMs()
        if let c = container { diag = DiagLog(url: c.appendingPathComponent(Shared.diagFile)) }
        let defaults = UserDefaults(suiteName: Shared.appGroup)
        let server = defaults?.string(forKey: Shared.serverKey) ?? Shared.defaultServer
        let userPrefetch = defaults?.bool(forKey: "prefetch") ?? false
        diag?.write("broadcast_start", ["server_set": String(!server.isEmpty), "prefetch": String(userPrefetch),
                                        "mem_avail_mb": memMB()])

        // Audio leaves the phone only over TLS.
        guard let url = URL(string: server), url.scheme == "https" else {
            diag?.write("no_server", ["detail": "set an https server URL in the Asterisk app"])
            finishBroadcastWithError(NSError(domain: "Asterisk", code: 1, userInfo: [
                NSLocalizedDescriptionKey: "Open Asterisk and set the server address first."]))
            return
        }
        let t = HTTPTransport(base: url)
        transport = t

        // Remote config with a short timeout; defaults if the server is slow.
        let sem = DispatchSemaphore(value: 0)
        var fetched: CaptureConfig?
        Task { fetched = await t.fetchConfig(); sem.signal() }
        _ = sem.wait(timeout: .now() + 3)
        config = (fetched ?? CaptureConfig()).applying(userPrefetch: userPrefetch)
        diag?.write("config", ["remote": String(fetched != nil), "dwellMs": String(config.dwellMs),
                               "strip": "\(config.strip.x),\(config.strip.y),\(config.strip.w),\(config.strip.h)"])

        let log = diag
        queue.sync {
            pipeline = CapturePipeline(config: config, transport: t) { ev, fields in log?.write(ev, fields) }
        }
        observeCheckRequests()
        startTimer()
        writeStatus(running: true)
    }

    override func broadcastPaused() { diag?.write("paused") }
    override func broadcastResumed() { diag?.write("resumed") }

    override func broadcastFinished() {
        diag?.write("broadcast_end", ["secs": String((DiagLog.nowMs() - startedMs) / 1000)])
        timer?.cancel()
        CFNotificationCenterRemoveEveryObserver(CFNotificationCenterGetDarwinNotifyCenter(),
                                                Unmanaged.passUnretained(self).toOpaque())
        queue.sync { pipeline?.stop(nowMs: DiagLog.nowMs()) }
        writeStatus(running: false)
        shipDiag()
    }

    // MARK: samples

    override func processSampleBuffer(_ sb: CMSampleBuffer, with type: RPSampleBufferType) {
        switch type {
        case .audioApp: handleAudio(sb)
        case .video: handleVideo(sb)
        case .audioMic: break                       // never used
        @unknown default: break
        }
    }

    private func handleAudio(_ sb: CMSampleBuffer) {
        guard let fd = CMSampleBufferGetFormatDescription(sb),
              let asbd = CMAudioFormatDescriptionGetStreamBasicDescription(fd)?.pointee,
              let block = CMSampleBufferGetDataBuffer(sb) else { return }
        let flags = asbd.mFormatFlags
        let fmt = PCMFormat(
            sampleRate: asbd.mSampleRate,
            channels: Int(asbd.mChannelsPerFrame),
            sample: flags & kAudioFormatFlagIsFloat != 0 ? .float32 : .int16,
            bigEndian: flags & kAudioFormatFlagIsBigEndian != 0,
            interleaved: flags & kAudioFormatFlagIsNonInterleaved == 0)
        if !loggedFormat {
            loggedFormat = true
            diag?.write("audio_format", ["rate": String(asbd.mSampleRate), "ch": String(asbd.mChannelsPerFrame),
                                         "bits": String(asbd.mBitsPerChannel), "flags": String(flags)])
        }
        guard asbd.mBitsPerChannel == 16 || asbd.mBitsPerChannel == 32 else { return }
        let len = CMBlockBufferGetDataLength(block)
        var data = Data(count: len)
        let status = data.withUnsafeMutableBytes { CMBlockBufferCopyDataBytes(block, atOffset: 0, dataLength: len, destination: $0.baseAddress!) }
        guard status == kCMBlockBufferNoErr else { return }
        queue.async { [weak self] in
            guard let self, let p = self.pipeline else { return }
            let pcm = self.resampler.convert(data, format: fmt)
            self.audioBuffers += 1
            self.audioSamplesOut += pcm.count
            p.onAudio(pcm)
        }
    }

    private func handleVideo(_ sb: CMSampleBuffer) {
        videoFrames += 1
        let now = DiagLog.nowMs()
        guard now - lastFrameMs >= config.frameIntervalMs,
              let px = CMSampleBufferGetImageBuffer(sb) else { return }
        lastFrameMs = now
        let fmt = CVPixelBufferGetPixelFormatType(px)
        if !loggedPixelFormat {
            loggedPixelFormat = true
            diag?.write("video_format", ["fourcc": fourCC(fmt), "w": String(CVPixelBufferGetWidth(px)),
                                         "h": String(CVPixelBufferGetHeight(px)),
                                         "planar": String(CVPixelBufferIsPlanar(px))])
        }
        // ReplayKit delivers 420 bi-planar; plane 0 is luma. Anything else is logged and skipped.
        guard CVPixelBufferIsPlanar(px) else { return }
        CVPixelBufferLockBaseAddress(px, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(px, .readOnly) }
        guard let base = CVPixelBufferGetBaseAddressOfPlane(px, 0) else { return }
        let w = CVPixelBufferGetWidthOfPlane(px, 0), h = CVPixelBufferGetHeightOfPlane(px, 0)
        let bpr = CVPixelBufferGetBytesPerRowOfPlane(px, 0)
        let c = config
        let sig = StripSignature.from(
            luma: UnsafeRawBufferPointer(start: base, count: bpr * h), width: w, height: h, bytesPerRow: bpr,
            region: c.strip, gridW: c.gridW, gridH: c.gridH, bright: UInt8(clamping: c.bright), stride: c.pixelStride)
        // Only the ~150-number signature crosses to the queue; no pixels are kept.
        queue.async { [weak self] in
            guard let self, let p = self.pipeline else { return }
            self.framesAnalysed += 1
            p.onFrame(sig, nowMs: now)
        }
    }

    // MARK: tap

    private func observeCheckRequests() {
        let center = CFNotificationCenterGetDarwinNotifyCenter()
        let me = Unmanaged.passUnretained(self).toOpaque()
        CFNotificationCenterAddObserver(center, me, { _, observer, _, _, _ in
            guard let observer else { return }
            Unmanaged<SampleHandler>.fromOpaque(observer).takeUnretainedValue().onCheckRequested()
        }, Shared.checkRequested as CFString, nil, .deliverImmediately)
    }

    private func onCheckRequested() {
        let t0 = DiagLog.nowMs()
        diag?.write("check_signal", [:], nowMs: t0)
        queue.async { [weak self] in
            guard let self, let p = self.pipeline else { return }
            self.diag?.write("check_on_queue", ["lag_ms": String(DiagLog.nowMs() - t0), "key": p.currentKey ?? "-",
                                                "dwell": String(p.dwellReached)])
            p.tap { [weak self] outcome in
                self?.publish(outcome, requestedMs: t0)
            }
        }
    }

    private func publish(_ outcome: CheckOutcome, requestedMs: Int64) {
        var obj: [String: Any] = ["at": DiagLog.nowMs(), "requested": requestedMs]
        switch outcome {
        case .verdict(let json):
            obj["kind"] = "verdict"
            obj["body"] = (try? JSONSerialization.jsonObject(with: json)) ?? [:]
        case .notReady(let why): obj["kind"] = "not_ready"; obj["detail"] = why
        case .silent: obj["kind"] = "silent"
        case .failed(let why): obj["kind"] = "failed"; obj["detail"] = why
        }
        diag?.write("result", ["kind": obj["kind"] as? String ?? "?",
                               "ms": String(DiagLog.nowMs() - requestedMs)])
        if let c = container, let data = try? JSONSerialization.data(withJSONObject: obj) {
            try? data.write(to: c.appendingPathComponent(Shared.resultFile), options: .atomic)
        }
        CFNotificationCenterPostNotification(CFNotificationCenterGetDarwinNotifyCenter(),
                                             CFNotificationName(Shared.resultReady as CFString), nil, nil, true)
        shipDiag()
    }

    // MARK: timer, status, diagnostics

    private func startTimer() {
        let t = DispatchSource.makeTimerSource(queue: queue)
        t.schedule(deadline: .now() + .milliseconds(250), repeating: .milliseconds(250))
        var ticks = 0
        t.setEventHandler { [weak self] in
            guard let self else { return }
            self.pipeline?.tick(nowMs: DiagLog.nowMs())
            ticks += 1
            if ticks % 8 == 0 { self.writeStatus(running: true) }          // ~2 s heartbeat
            if ticks % 40 == 0 {                                          // ~10 s
                self.diag?.write("heartbeat", [
                    "audio_bufs": String(self.audioBuffers), "samples16k": String(self.audioSamplesOut),
                    "video_frames": String(self.videoFrames), "analysed": String(self.framesAnalysed),
                    "key": self.pipeline?.currentKey ?? "-", "mem_avail_mb": self.memMB()])
                self.shipDiag()
            }
        }
        t.resume()
        timer = t
    }

    private func writeStatus(running: Bool) {
        guard let c = container else { return }
        let obj: [String: Any] = ["running": running, "beat": DiagLog.nowMs(), "since": startedMs,
                                  "key": pipeline?.currentKey ?? NSNull(),
                                  "dwell": pipeline?.dwellReached ?? false]
        if let data = try? JSONSerialization.data(withJSONObject: obj) {
            try? data.write(to: c.appendingPathComponent(Shared.statusFile), options: .atomic)
        }
    }

    private func shipDiag() {
        guard let t = transport, let lines = diag?.takeUnsent(), !lines.isEmpty else { return }
        Task { await t.postDiag(lines, source: "broadcast") }
    }

    private func memMB() -> String { String(os_proc_available_memory() / 1_048_576) }

    private func fourCC(_ v: OSType) -> String {
        let b = [24, 16, 8, 0].map { UInt8((v >> $0) & 0xFF) }
        return String(bytes: b, encoding: .ascii) ?? String(v)
    }
}
