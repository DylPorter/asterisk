import { StatusBar } from 'expo-status-bar';
import { useCallback, useEffect, useState } from 'react';
import {
  Pressable, SafeAreaView, ScrollView, StyleSheet, Switch, Text, TextInput, View,
} from 'react-native';
import { AsteriskBridge, BroadcastPicker } from './modules/asterisk-bridge/src';

type Status = { running: boolean; beat: number; key: string | null; dwell: boolean };

function parse<T>(s: string | null): T | null {
  if (!s) return null;
  try { return JSON.parse(s) as T; } catch { return null; }
}

/**
 * Test-build screen. Three jobs: set the server, start the broadcast, and show
 * the diagnostics the extensions write, so a test run on a phone can be read
 * on the phone itself as well as on the server.
 */
export default function App() {
  const [server, setServer] = useState(AsteriskBridge.getServer());
  const [prefetch, setPrefetch] = useState(AsteriskBridge.getPrefetch());
  const [status, setStatus] = useState<Status | null>(null);
  const [result, setResult] = useState<string | null>(null);
  const [diag, setDiag] = useState<string[]>([]);

  const refresh = useCallback(() => {
    setStatus(parse<Status>(AsteriskBridge.status()));
    setResult(AsteriskBridge.lastResult());
    setDiag(AsteriskBridge.diagTail(80));
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 2000);
    return () => clearInterval(t);
  }, [refresh]);

  const live = !!status?.running && Date.now() - status.beat < 6000;
  const httpsOk = server.startsWith('https://');

  return (
    <SafeAreaView style={s.root}>
      <StatusBar style="auto" />
      <ScrollView contentContainerStyle={s.body}>
        <Text style={s.h1}>Asterisk ✱</Text>

        <Text style={s.label}>Server (https)</Text>
        <TextInput
          style={s.input} value={server} onChangeText={setServer}
          autoCapitalize="none" autoCorrect={false} keyboardType="url"
          placeholder="https://…" onEndEditing={() => AsteriskBridge.setServer(server.trim())}
        />
        {!httpsOk && server.length > 0 && <Text style={s.warn}>Must start with https://</Text>}

        <View style={s.row}>
          <View style={{ flex: 1 }}>
            <Text style={s.label}>Prepare in the background</Text>
            <Text style={s.small}>
              Uploads audio after 5 s on a video so the check is faster. iPhone can't limit this to
              TikTok/Reels/Shorts, so anything playing for 5 s is sent. Off: audio leaves only when you tap.
            </Text>
          </View>
          <Switch value={prefetch} onValueChange={(v) => { setPrefetch(v); AsteriskBridge.setPrefetch(v); }} />
        </View>

        <View style={s.pickerWrap}>
          <BroadcastPicker style={StyleSheet.absoluteFill} />
          <View pointerEvents="none" style={[s.button, live && s.buttonLive]}>
            <Text style={s.buttonText}>{live ? 'Running: tap to stop' : 'Start Asterisk'}</Text>
          </View>
        </View>
        <Text style={s.small}>
          {live
            ? `Watching: ${status?.key ?? 'no video yet'}${status?.dwell ? ' (ready to check)' : ''}`
            : 'Not running. Start it, then open TikTok, Reels or Shorts.'}
        </Text>

        <Text style={s.h2}>Trigger</Text>
        <Text style={s.small}>
          Settings → Accessibility → Touch → Back Tap → Double Tap → “Check video”. If it isn't listed,
          add the “Check this video” action in the Shortcuts app and pick that shortcut instead.
        </Text>

        <Text style={s.h2}>Last result</Text>
        <Text style={s.mono}>{result ?? '—'}</Text>

        <View style={s.row}>
          <Text style={s.h2}>Diagnostics</Text>
          <Pressable onPress={() => { AsteriskBridge.clearDiag(); refresh(); }}>
            <Text style={s.link}>Clear</Text>
          </Pressable>
        </View>
        {diag.slice().reverse().map((l, i) => <Text key={i} style={s.mono}>{l}</Text>)}
      </ScrollView>
    </SafeAreaView>
  );
}

const s = StyleSheet.create({
  root: { flex: 1 },
  body: { padding: 16, gap: 10 },
  h1: { fontSize: 28, fontWeight: '700' },
  h2: { fontSize: 18, fontWeight: '600', marginTop: 12 },
  label: { fontSize: 14, fontWeight: '600' },
  small: { fontSize: 12, opacity: 0.7 },
  warn: { fontSize: 12, color: '#c0392b' },
  input: { borderWidth: 1, borderColor: '#8884', borderRadius: 8, padding: 10, fontSize: 15 },
  row: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', gap: 12 },
  pickerWrap: { height: 52, marginTop: 8 },
  button: {
    position: 'absolute', top: 0, left: 0, right: 0, bottom: 0, borderRadius: 12, backgroundColor: '#222',
    alignItems: 'center', justifyContent: 'center',
  },
  buttonLive: { backgroundColor: '#c0392b' },
  buttonText: { color: 'white', fontSize: 16, fontWeight: '600' },
  link: { color: '#2e86de' },
  mono: { fontFamily: 'Menlo', fontSize: 10 },
});
