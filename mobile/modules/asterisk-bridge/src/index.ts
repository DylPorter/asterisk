import { requireNativeModule, requireNativeView } from 'expo';
import type { ViewProps } from 'react-native';

type Bridge = {
  setServer(url: string): void;
  getServer(): string;
  setPrefetch(on: boolean): void;
  getPrefetch(): boolean;
  status(): string | null;
  lastResult(): string | null;
  diagTail(n: number): string[];
  clearDiag(): void;
};

export const AsteriskBridge = requireNativeModule<Bridge>('AsteriskBridge');
export const BroadcastPicker = requireNativeView<ViewProps>('AsteriskBridge');
