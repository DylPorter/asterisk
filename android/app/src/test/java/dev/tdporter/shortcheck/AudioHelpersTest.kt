package dev.tdporter.shortcheck

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.nio.ByteBuffer
import java.nio.ByteOrder

class AudioHelpersTest {

    private fun seq(from: Int, n: Int) = ShortArray(n) { (from + it).toShort() }

    @Test fun `ring returns everything since an offset`() {
        val r = PcmRing(10)
        r.write(seq(0, 4))
        val d = r.since(0)
        assertArrayEquals(seq(0, 4), d.samples)
        assertEquals(4L, d.end)
        r.write(seq(4, 3))
        assertArrayEquals(seq(4, 3), r.since(d.end).samples)
    }

    @Test fun `ring drain at the end is empty`() {
        val r = PcmRing(10)
        r.write(seq(0, 5))
        assertEquals(0, r.since(5).samples.size)
        assertEquals(5L, r.since(5).end)
    }

    @Test fun `ring wraps and keeps only the newest capacity samples`() {
        val r = PcmRing(10)
        r.write(seq(0, 7))
        r.write(seq(7, 8))                     // 15 written, 0..4 overwritten
        val d = r.since(0)
        assertArrayEquals(seq(5, 10), d.samples)
        assertEquals(15L, d.end)
        assertArrayEquals(seq(12, 3), r.since(12).samples)
    }

    @Test fun `oversized single write keeps its tail`() {
        val r = PcmRing(4)
        r.write(seq(0, 9))
        assertArrayEquals(seq(5, 4), r.since(0).samples)
        assertEquals(9L, r.written)
    }

    @Test fun `partial write count is honoured`() {
        val r = PcmRing(10)
        r.write(seq(0, 8), 3)
        assertArrayEquals(seq(0, 3), r.since(0).samples)
    }

    @Test fun `clear resets offsets so a new reel starts at zero`() {
        val r = PcmRing(10)
        r.write(seq(0, 6))
        r.clear()
        assertEquals(0, r.since(0).samples.size)
        r.write(seq(100, 2))
        assertArrayEquals(seq(100, 2), r.since(0).samples)
    }

    @Test fun `wav header is the canonical 44 bytes`() {
        val pcm = shortArrayOf(1, -1, 32767, -32768)
        val wav = encodeWav(pcm, 16_000)
        assertEquals(44 + 8, wav.size)
        val bb = ByteBuffer.wrap(wav).order(ByteOrder.LITTLE_ENDIAN)
        assertEquals("RIFF", String(wav, 0, 4, Charsets.US_ASCII))
        assertEquals(36 + 8, bb.getInt(4))
        assertEquals("WAVE", String(wav, 8, 4, Charsets.US_ASCII))
        assertEquals("fmt ", String(wav, 12, 4, Charsets.US_ASCII))
        assertEquals(16, bb.getInt(16))
        assertEquals(1, bb.getShort(20).toInt())          // PCM
        assertEquals(1, bb.getShort(22).toInt())          // mono
        assertEquals(16_000, bb.getInt(24))
        assertEquals(32_000, bb.getInt(28))
        assertEquals(2, bb.getShort(32).toInt())
        assertEquals(16, bb.getShort(34).toInt())
        assertEquals("data", String(wav, 36, 4, Charsets.US_ASCII))
        assertEquals(8, bb.getInt(40))
        assertEquals(1, bb.getShort(44).toInt())
        assertEquals(-1, bb.getShort(46).toInt())
        assertEquals(32767, bb.getShort(48).toInt())
        assertEquals(-32768, bb.getShort(50).toInt())
    }

    @Test fun `rms of digital silence is below the silence threshold`() {
        assertEquals(Double.NEGATIVE_INFINITY, pcmRmsDb(ShortArray(1600)), 0.0)
        assertEquals(Double.NEGATIVE_INFINITY, pcmRmsDb(ShortArray(0)), 0.0)
        assertTrue(pcmRmsDb(ShortArray(1600)) < Uploader.SILENT_DB)
    }

    @Test fun `rms of full scale square wave is about 0 dBFS`() {
        val sq = ShortArray(1000) { if (it % 2 == 0) 32767 else -32768 }
        assertEquals(0.0, pcmRmsDb(sq), 0.01)
    }

    @Test fun `quiet dither is silent and speech level is not`() {
        // +-1 LSB noise is about -90 dBFS: what an opted-out app looks like.
        val dither = ShortArray(1600) { if (it % 2 == 0) 1 else -1 }
        assertTrue(pcmRmsDb(dither) < Uploader.SILENT_DB)
        // Amplitude 1000 (~-30 dBFS) is ordinary quiet speech.
        val speech = ShortArray(1600) { if (it % 2 == 0) 1000 else -1000 }
        assertTrue(pcmRmsDb(speech) > Uploader.SILENT_DB)
    }
}
