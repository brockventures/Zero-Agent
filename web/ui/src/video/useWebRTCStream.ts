import { useEffect, useRef } from 'react';
import type { VideoSource } from '../sse/types';

export function tryUnref(timer: unknown): void {
  if (!timer) return;
  const unrefable = timer as { unref?: () => void };
  if (typeof unrefable.unref === 'function') {
    unrefable.unref();
  }
}

export function applyZeroLatencyPlayoutDelay(receiver?: RTCRtpReceiver | null): void {
  if (!receiver) return;
  try {
    if ('playoutDelayHint' in receiver) {
      (receiver as { playoutDelayHint?: number }).playoutDelayHint = 0;
    }
  } catch {
    // Non-fatal if browser blocks or throws on playoutDelayHint
  }
}

export function safePlay(video: HTMLVideoElement | null): void {
  if (!video || typeof video.play !== 'function') return;
  try {
    const p = video.play();
    if (p !== undefined && typeof p.catch === 'function') {
      p.catch((err: unknown) => {
        console.warn('[MirrormereVideo] Playback prevented by browser autoplay policy:', err);
      });
    }
  } catch {
    // ignore
  }
}

export interface UseWebRTCStreamOptions {
  videoRef: React.RefObject<HTMLVideoElement | null>;
  stream: VideoSource | null;
  enabled?: boolean;
  fetchFn?: typeof fetch;
  RTCPeerConnectionClass?: typeof RTCPeerConnection;
  negotiationTimeout?: number;
  disconnectTimeout?: number;
  maxReconnectAttempts?: number;
  reconnectBaseDelay?: number;
  reconnectBackoffFactor?: number;
  onFallback?: () => void;
  onConnected?: () => void;
}

/**
 * Custom hook managing WebRTC WHEP negotiation, zero-latency playout delay,
 * ICE watchdog, auto-reconnect backoff, and hardware decode buffer flushing.
 */
export function useWebRTCStream({
  videoRef,
  stream,
  enabled = true,
  fetchFn,
  RTCPeerConnectionClass,
  negotiationTimeout = 5000,
  disconnectTimeout = 2000,
  maxReconnectAttempts = 3,
  reconnectBaseDelay = 1000,
  reconnectBackoffFactor = 1.5,
  onFallback,
  onConnected,
}: UseWebRTCStreamOptions): void {
  const streamId = stream?.id;
  const streamUrl = stream?.stream_url;
  const streamType = stream?.type;

  const onFallbackRef = useRef(onFallback);
  const onConnectedRef = useRef(onConnected);
  const fetchRef = useRef(fetchFn);
  const pcClassRef = useRef(RTCPeerConnectionClass);

  useEffect(() => {
    onFallbackRef.current = onFallback;
    onConnectedRef.current = onConnected;
    fetchRef.current = fetchFn;
    pcClassRef.current = RTCPeerConnectionClass;
  }, [onFallback, onConnected, fetchFn, RTCPeerConnectionClass]);

  useEffect(() => {
    if (!enabled || !streamUrl) {
      return;
    }

    let cancelled = false;
    let pc: RTCPeerConnection | null = null;
    let mountedVideoEl: HTMLVideoElement | null = videoRef.current;
    let reconnectAttempts = 0;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let disconnectTimer: ReturnType<typeof setTimeout> | null = null;

    const PeerConnection =
      pcClassRef.current ||
      (typeof RTCPeerConnection !== 'undefined' ? RTCPeerConnection : null);

    const closePC = (targetPC: RTCPeerConnection | null): void => {
      if (!targetPC) return;
      targetPC.oniceconnectionstatechange = null;
      targetPC.onconnectionstatechange = null;
      targetPC.ontrack = null;
      targetPC.onicecandidate = null;
      if (typeof targetPC.close === 'function') {
        try {
          targetPC.close();
        } catch {
          // ignore
        }
      }
    };

    const flushHardwareBuffer = (): void => {
      const videoEl = videoRef.current ?? mountedVideoEl;
      if (videoEl) {
        videoEl.srcObject = null;
        if (typeof videoEl.load === 'function') {
          try {
            videoEl.load();
          } catch {
            // ignore
          }
        }
      }
    };

    const scheduleReconnect = (): void => {
      if (cancelled || reconnectTimer) return;

      if (reconnectAttempts >= maxReconnectAttempts) {
        console.warn(
          `[MirrormereVideo] WebRTC stream ${streamId ?? 'unknown'} reconnect attempts exhausted (${maxReconnectAttempts}). Falling back.`,
        );
        onFallbackRef.current?.();
        return;
      }

      reconnectAttempts++;
      const delay = Math.round(
        reconnectBaseDelay * Math.pow(reconnectBackoffFactor, reconnectAttempts - 1),
      );

      reconnectTimer = setTimeout(async () => {
        reconnectTimer = null;
        if (cancelled) return;
        await reconnect();
      }, delay);
      tryUnref(reconnectTimer);
    };

    const handleConnectionChange = (currentPC: RTCPeerConnection): void => {
      if (cancelled || !currentPC) return;

      const connState = currentPC.connectionState;
      const iceState = currentPC.iceConnectionState;

      // 1. Healthy connected
      if (connState === 'connected' || iceState === 'connected' || iceState === 'completed') {
        if (disconnectTimer) {
          clearTimeout(disconnectTimer);
          disconnectTimer = null;
        }
        if (reconnectTimer) {
          clearTimeout(reconnectTimer);
          reconnectTimer = null;
        }
        reconnectAttempts = 0;
        onConnectedRef.current?.();
        return;
      }

      // 2. In progress
      if (connState === 'connecting' || iceState === 'checking') {
        if (disconnectTimer) {
          clearTimeout(disconnectTimer);
          disconnectTimer = null;
        }
        return;
      }

      // 3. Failed
      if (connState === 'failed' || iceState === 'failed') {
        if (disconnectTimer) {
          clearTimeout(disconnectTimer);
          disconnectTimer = null;
        }
        scheduleReconnect();
        return;
      }

      // 4. Disconnected with 2000ms grace period
      if (connState === 'disconnected' || iceState === 'disconnected') {
        if (!disconnectTimer) {
          disconnectTimer = setTimeout(() => {
            disconnectTimer = null;
            if (cancelled) return;
            scheduleReconnect();
          }, disconnectTimeout);
          tryUnref(disconnectTimer);
        }
        return;
      }

      // 5. Closed unexpectedly
      if ((connState === 'closed' || iceState === 'closed') && !cancelled) {
        scheduleReconnect();
      }
    };

    const negotiate = async (currentPC: RTCPeerConnection): Promise<void> => {
      let timeoutId: ReturnType<typeof setTimeout> | null = null;
      try {
        const offer = await currentPC.createOffer();
        if (cancelled || pc !== currentPC) {
          closePC(currentPC);
          return;
        }

        await currentPC.setLocalDescription(offer);
        if (cancelled || pc !== currentPC) {
          closePC(currentPC);
          return;
        }

        // Wait for ICE gathering completion or 500ms safety timeout
        await new Promise<void>((resolve) => {
          if (currentPC.iceGatheringState === 'complete') return resolve();
          let settled = false;
          let iceTimer: ReturnType<typeof setTimeout> | null = null;

          const cleanup = (): void => {
            if (iceTimer) {
              clearTimeout(iceTimer);
              iceTimer = null;
            }
            if (typeof currentPC.removeEventListener === 'function') {
              currentPC.removeEventListener('icegatheringstatechange', check);
            }
          };

          const check = (): void => {
            if (currentPC.iceGatheringState === 'complete' && !settled) {
              settled = true;
              cleanup();
              resolve();
            }
          };

          if (typeof currentPC.addEventListener === 'function') {
            currentPC.addEventListener('icegatheringstatechange', check);
          }

          iceTimer = setTimeout(() => {
            if (!settled) {
              settled = true;
              cleanup();
              resolve();
            }
          }, 500);
          tryUnref(iceTimer);
        });

        if (cancelled || pc !== currentPC) {
          closePC(currentPC);
          return;
        }

        const sdpPayload = currentPC.localDescription ? currentPC.localDescription.sdp : offer.sdp;
        const effectiveFetch =
          fetchRef.current ||
          (typeof window !== 'undefined' && typeof window.fetch === 'function'
            ? window.fetch.bind(window)
            : fetch);

        if (!effectiveFetch) return;

        const controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
        if (controller) {
          timeoutId = setTimeout(() => controller.abort(), negotiationTimeout);
          tryUnref(timeoutId);
        }

        const res = await effectiveFetch(streamUrl, {
          method: 'POST',
          headers: { 'Content-Type': 'application/sdp' },
          body: sdpPayload,
          signal: controller ? controller.signal : undefined,
        });

        if (timeoutId) {
          clearTimeout(timeoutId);
          timeoutId = null;
        }

        if (cancelled || pc !== currentPC) {
          closePC(currentPC);
          return;
        }

        if (!res.ok) {
          throw new Error(`HTTP ${res.status}`);
        }

        const text = await res.text();
        if (cancelled || pc !== currentPC) {
          closePC(currentPC);
          return;
        }

        let answerSDP = text;
        try {
          const json = JSON.parse(text) as { sdp?: string };
          if (json && typeof json.sdp === 'string') {
            answerSDP = json.sdp;
          }
        } catch {
          // Payload is raw SDP text
        }

        await currentPC.setRemoteDescription({ type: 'answer', sdp: answerSDP });
        if (typeof currentPC.getReceivers === 'function') {
          for (const receiver of currentPC.getReceivers()) {
            if (receiver?.track?.kind === 'video') {
              applyZeroLatencyPlayoutDelay(receiver);
            }
          }
        }
      } catch (err: unknown) {
        if (timeoutId) {
          clearTimeout(timeoutId);
        }
        if (!cancelled) {
          console.warn('[MirrormereVideo] WebRTC SDP negotiation failed:', err);
          closePC(currentPC);
          if (pc === currentPC) pc = null;
          scheduleReconnect();
        }
      }
    };

    const connect = async (): Promise<void> => {
      if (cancelled || !PeerConnection) return;

      try {
        const newPC = new PeerConnection();
        pc = newPC;

        if (typeof newPC.addTransceiver === 'function') {
          const videoTransceiver = newPC.addTransceiver('video', { direction: 'recvonly' });
          newPC.addTransceiver('audio', { direction: 'recvonly' });
          if (videoTransceiver && videoTransceiver.receiver) {
            applyZeroLatencyPlayoutDelay(videoTransceiver.receiver);
          }
        }

        newPC.ontrack = (event: RTCTrackEvent) => {
          if (cancelled) return;
          if (event.receiver && event.track && event.track.kind === 'video') {
            applyZeroLatencyPlayoutDelay(event.receiver);
          }
          const videoEl = videoRef.current;
          if (!videoEl) return;
          mountedVideoEl = videoEl;

          if (event.streams && event.streams[0]) {
            videoEl.srcObject = event.streams[0];
          } else {
            const MediaStreamConstructor =
              typeof MediaStream !== 'undefined'
                ? MediaStream
                : typeof window !== 'undefined'
                  ? window.MediaStream
                  : null;
            if (!videoEl.srcObject && MediaStreamConstructor) {
              videoEl.srcObject = new MediaStreamConstructor();
            }
            if (
              videoEl.srcObject &&
              typeof (videoEl.srcObject as MediaStream).addTrack === 'function' &&
              event.track
            ) {
              (videoEl.srcObject as MediaStream).addTrack(event.track);
            }
          }
        };

        const onConnChange = (): void => handleConnectionChange(newPC);
        newPC.oniceconnectionstatechange = onConnChange;
        newPC.onconnectionstatechange = onConnChange;

        await negotiate(newPC);
      } catch (err: unknown) {
        console.warn('[MirrormereVideo] RTCPeerConnection initialization error:', err);
        if (!cancelled) {
          scheduleReconnect();
        }
      }

      safePlay(videoRef.current);
    };

    const reconnect = async (): Promise<void> => {
      if (cancelled) return;
      closePC(pc);
      pc = null;
      flushHardwareBuffer();
      await connect();
    };

    void connect();

    return () => {
      cancelled = true;
      if (reconnectTimer) {
        clearTimeout(reconnectTimer);
      }
      if (disconnectTimer) {
        clearTimeout(disconnectTimer);
      }
      closePC(pc);
      pc = null;
      flushHardwareBuffer();
    };
  }, [
    enabled,
    streamId,
    streamUrl,
    streamType,
    videoRef,
    negotiationTimeout,
    disconnectTimeout,
    maxReconnectAttempts,
    reconnectBaseDelay,
    reconnectBackoffFactor,
  ]);
}
