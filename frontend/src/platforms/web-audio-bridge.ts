import { BaseInputAudioSession } from "../bases/audio-session";
import type { InputAudioSessionConfig } from "../bases/audio-session";
import { WebInputAudioSession } from "./web";

export {
    WEB_AUDIO_BRIDGE_SAMPLE_RATE,
    WebBridgeInputAudioSession,
    createWebAudioBridge,
};
export type {
    WebAudioBridge,
    WebAudioBridgePublishOptions,
    WebAudioBridgeUserInputConfig,
    WebBridgeInputAudioSessionConfig,
    WebBridgeParticipantId,
};

/**
 * Default shared-stream sample rate used by the web audio bridge.
 */
const WEB_AUDIO_BRIDGE_SAMPLE_RATE = 16000;
const BRIDGE_FRAME_SAMPLES = 512;
const DEFAULT_VAD_REDEMPTION_MS = 500;
const DEFAULT_USER_SOURCE_ID = "user";

/**
 * Unique identifier for one participant publishing into or receiving from the
 * shared web bridge audio stream.
 */
type WebBridgeParticipantId = string;

/**
 * User-input capture options accepted by ``openUserInput()``.
 */
interface WebAudioBridgeUserInputConfig {
    /**
     * Source identifier used when publishing microphone audio into the bridge.
     */
    sourceId?: WebBridgeParticipantId;
    /**
     * Requested capture sample rate before bridge normalization.
     */
    sampleRate?: number;
    /**
     * Whether client-side VAD should emit speech start/end for microphone audio.
     */
    enableVAD?: boolean;
    /**
     * Whether client-side speech enhancement should run before publishing frames.
     */
    enableEnhancer?: boolean;
    /**
     * Redemption window used by the client VAD before emitting speech end.
     */
    vadRedemptionMs?: number;
}

/**
 * Metadata attached to one published bridge audio chunk.
 */
interface WebAudioBridgePublishOptions {
    /**
     * Source identifier responsible for the published audio.
     */
    sourceId: WebBridgeParticipantId;
    /**
     * Sample rate of the published PCM payload.
     */
    sampleRate: number;
}

/**
 * Public web-only bridge API used to feed shared audio into bot sessions.
 */
interface WebAudioBridge {
    /**
     * Open browser microphone capture and publish user audio into the bridge.
     *
     * @param config Optional user-input capture overrides.
     */
    openUserInput(config?: WebAudioBridgeUserInputConfig): Promise<void>;
    /**
     * Stop browser microphone capture previously opened through the bridge.
     */
    closeUserInput(): Promise<void>;
    /**
     * Publish one PCM s16le mono chunk into the shared bridge stream.
     *
     * @param pcmChunkInt16 PCM chunk to mix into the shared stream.
     * @param options Source metadata for the chunk.
     */
    publishAudio(
        pcmChunkInt16: ArrayBuffer,
        options: WebAudioBridgePublishOptions,
    ): void;
    /**
     * Broadcast a speech-start event for one source into the shared stream.
     *
     * @param sourceId Source that just became active.
     */
    publishSpeechStart(sourceId: WebBridgeParticipantId): void;
    /**
     * Broadcast a speech-end event for one source into the shared stream.
     *
     * @param sourceId Source that just became inactive.
     */
    publishSpeechEnd(sourceId: WebBridgeParticipantId): void;
    /**
     * Stop the bridge, microphone capture, and the shared silent-stream loop.
     */
    close(): Promise<void>;
}

/**
 * Input-session config for sessions that consume the shared web bridge stream.
 */
interface WebBridgeInputAudioSessionConfig extends InputAudioSessionConfig {
    /**
     * Must be ``"web_bridge"`` to consume shared bridge audio instead of the microphone.
     */
    mode: "web_bridge";
    /**
     * Unique participant id used for diagnostics and source-specific VAD defaults.
     */
    participantId: WebBridgeParticipantId;
    /**
     * Shared web bridge that will feed this input session.
     */
    bridge: WebAudioBridge;
    /**
     * Whether this participant should auto-broadcast VAD when publishing its own output back
     * into the bridge.
     */
    autoEmitVad?: boolean;
    /**
     * Redemption window used when auto-broadcasting source-local speech end.
     */
    vadRedemptionMs?: number;
}

type WebAudioBridgeInputTarget = {
    participantId: WebBridgeParticipantId;
    sampleRate: number;
    autoEmitVad: boolean;
    vadRedemptionMs: number;
    pushAudioChunk: (pcmChunkInt16: ArrayBuffer) => void | Promise<void>;
    handleSpeechStart: () => void | Promise<void>;
    handleSpeechEnd: () => void | Promise<void>;
};

interface WebAudioBridgeInternal extends WebAudioBridge {
    registerInputTarget(target: WebAudioBridgeInputTarget): void;
    unregisterInputTarget(participantId: WebBridgeParticipantId): void;
}

type SourceRuntimeState = {
    autoEmitVad: boolean;
    vadRedemptionMs: number;
    endTimerId: number | null;
};

/**
 * Input session that continuously consumes frames from the shared web audio bridge.
 */
class WebBridgeInputAudioSession extends BaseInputAudioSession {
    private readonly bridge: WebAudioBridgeInternal;
    private readonly config: WebBridgeInputAudioSessionConfig;
    private _muted = false;
    private opened = false;

    constructor(config: WebBridgeInputAudioSessionConfig) {
        super();
        this.config = {
            ...config,
            autoEmitVad: config.autoEmitVad ?? false,
            vadRedemptionMs: resolveVadRedemptionMs(config.vadRedemptionMs),
        };
        this.bridge = config.bridge as WebAudioBridgeInternal;
    }

    async open(): Promise<void> {
        if (this.opened) {
            throw new Error("Session already started");
        }
        this.opened = true;
        this.bridge.registerInputTarget({
            participantId: this.config.participantId,
            sampleRate: this.config.sampleRate,
            autoEmitVad: Boolean(this.config.autoEmitVad),
            vadRedemptionMs: resolveVadRedemptionMs(this.config.vadRedemptionMs),
            pushAudioChunk: (pcmChunkInt16) => {
                if (this._muted) {
                    return;
                }
                this.frameCallback(pcmChunkInt16);
            },
            handleSpeechStart: () => {
                if (this._muted) {
                    return;
                }
                this.speechStartCallback();
            },
            handleSpeechEnd: () => {
                if (this._muted) {
                    return;
                }
                this.speechEndCallback();
            },
        });
    }

    async close(): Promise<void> {
        if (!this.opened) {
            throw new Error("Session not started");
        }
        this.opened = false;
        this.bridge.unregisterInputTarget(this.config.participantId);
    }

    get muted(): boolean {
        return this._muted;
    }

    set muted(value: boolean) {
        this._muted = value;
    }
}

class DefaultWebAudioBridge implements WebAudioBridgeInternal {
    private readonly bridgeSampleRate = WEB_AUDIO_BRIDGE_SAMPLE_RATE;
    private readonly frameSamples = BRIDGE_FRAME_SAMPLES;
    private readonly subscribers = new Map<WebBridgeParticipantId, WebAudioBridgeInputTarget>();
    private readonly sourceStates = new Map<WebBridgeParticipantId, SourceRuntimeState>();
    private readonly activeVadSources = new Set<WebBridgeParticipantId>();
    private readonly sourceBuffers = new Map<WebBridgeParticipantId, number[]>();
    private dispatchTimerId: number | null = null;
    private userInputSession: WebInputAudioSession | null = null;
    private userInputSourceId: WebBridgeParticipantId | null = null;

    registerInputTarget(target: WebAudioBridgeInputTarget): void {
        this.subscribers.set(target.participantId, target);
        this.ensureSourceState(target.participantId, {
            autoEmitVad: target.autoEmitVad,
            vadRedemptionMs: target.vadRedemptionMs,
        });
        if (this.hasAudibleVadForTarget(target.participantId)) {
            void Promise.resolve(target.handleSpeechStart());
        }
        this.ensureDispatchLoop();
    }

    unregisterInputTarget(participantId: WebBridgeParticipantId): void {
        this.subscribers.delete(participantId);
        this.publishSpeechEnd(participantId);
        this.sourceBuffers.delete(participantId);
        const sourceState = this.sourceStates.get(participantId);
        if (sourceState?.endTimerId != null) {
            window.clearTimeout(sourceState.endTimerId);
        }
        this.sourceStates.delete(participantId);
        this.maybeStopDispatchLoop();
    }

    async openUserInput(
        config: WebAudioBridgeUserInputConfig = {},
    ): Promise<void> {
        await this.closeUserInput();

        const resolvedConfig = {
            sourceId: config.sourceId ?? DEFAULT_USER_SOURCE_ID,
            sampleRate: config.sampleRate ?? this.bridgeSampleRate,
            enableVAD: config.enableVAD ?? true,
            enableEnhancer: config.enableEnhancer ?? true,
            vadRedemptionMs: resolveVadRedemptionMs(config.vadRedemptionMs),
        };

        const inputSession = new WebInputAudioSession({
            sampleRate: resolvedConfig.sampleRate,
            enableVAD: resolvedConfig.enableVAD,
            enableEnhancer: resolvedConfig.enableEnhancer,
            vadRedemptionMs: resolvedConfig.vadRedemptionMs,
        });

        inputSession.onFrame((pcmChunkInt16) => {
            this.publishAudio(pcmChunkInt16, {
                sourceId: resolvedConfig.sourceId,
                sampleRate: resolvedConfig.sampleRate,
            });
        });
        inputSession.onSpeechStart(() => {
            this.publishSpeechStart(resolvedConfig.sourceId);
        });
        inputSession.onSpeechEnd(() => {
            this.publishSpeechEnd(resolvedConfig.sourceId);
        });

        await inputSession.open();
        this.userInputSession = inputSession;
        this.userInputSourceId = resolvedConfig.sourceId;
        this.ensureSourceState(resolvedConfig.sourceId, {
            autoEmitVad: resolvedConfig.enableVAD,
            vadRedemptionMs: resolvedConfig.vadRedemptionMs,
        });
        this.ensureDispatchLoop();
    }

    async closeUserInput(): Promise<void> {
        const current = this.userInputSession;
        const sourceId = this.userInputSourceId;
        this.userInputSession = null;
        this.userInputSourceId = null;

        if (current) {
            try {
                await current.close();
            } catch {
                // Ignore shutdown errors from already-closed microphone sessions.
            }
        }
        if (sourceId) {
            this.publishSpeechEnd(sourceId);
            this.sourceBuffers.delete(sourceId);
        }
        this.maybeStopDispatchLoop();
    }

    publishAudio(
        pcmChunkInt16: ArrayBuffer,
        options: WebAudioBridgePublishOptions,
    ): void {
        const sourceState = this.sourceStates.get(options.sourceId);
        const float32 = decodePcm16ToFloat32(pcmChunkInt16);
        const normalized = (
            options.sampleRate === this.bridgeSampleRate
                ? float32
                : resampleFloat32(float32, options.sampleRate, this.bridgeSampleRate)
        );

        if (normalized.length === 0) {
            return;
        }

        mixIntoSourceBuffer(
            this.sourceBuffers,
            options.sourceId,
            normalized,
        );
        this.ensureDispatchLoop();

        if (sourceState?.autoEmitVad) {
            this.publishSpeechStart(options.sourceId);
            if (sourceState.endTimerId != null) {
                window.clearTimeout(sourceState.endTimerId);
            }
            sourceState.endTimerId = window.setTimeout(() => {
                sourceState.endTimerId = null;
                this.publishSpeechEnd(options.sourceId);
            }, sourceState.vadRedemptionMs);
        }
    }

    publishSpeechStart(sourceId: WebBridgeParticipantId): void {
        const previouslyAudibleTargets = new Set<WebBridgeParticipantId>();
        for (const target of this.subscribers.values()) {
            if (this.hasAudibleVadForTarget(target.participantId)) {
                previouslyAudibleTargets.add(target.participantId);
            }
        }
        this.activeVadSources.add(sourceId);
        for (const target of this.subscribers.values()) {
            if (previouslyAudibleTargets.has(target.participantId)) {
                continue;
            }
            if (!this.hasAudibleVadForTarget(target.participantId)) {
                continue;
            }
            void Promise.resolve(target.handleSpeechStart());
        }
    }

    publishSpeechEnd(sourceId: WebBridgeParticipantId): void {
        const sourceState = this.sourceStates.get(sourceId);
        if (sourceState?.endTimerId != null) {
            window.clearTimeout(sourceState.endTimerId);
            sourceState.endTimerId = null;
        }
        const previouslyAudibleTargets = new Set<WebBridgeParticipantId>();
        for (const target of this.subscribers.values()) {
            if (this.hasAudibleVadForTarget(target.participantId)) {
                previouslyAudibleTargets.add(target.participantId);
            }
        }
        const wasActive = this.activeVadSources.delete(sourceId);
        if (!wasActive) {
            return;
        }
        for (const target of this.subscribers.values()) {
            if (!previouslyAudibleTargets.has(target.participantId)) {
                continue;
            }
            if (this.hasAudibleVadForTarget(target.participantId)) {
                continue;
            }
            void Promise.resolve(target.handleSpeechEnd());
        }
    }

    async close(): Promise<void> {
        await this.closeUserInput();
        if (this.dispatchTimerId != null) {
            window.clearInterval(this.dispatchTimerId);
            this.dispatchTimerId = null;
        }
        for (const sourceState of this.sourceStates.values()) {
            if (sourceState.endTimerId != null) {
                window.clearTimeout(sourceState.endTimerId);
            }
        }
        this.sourceStates.clear();
        this.activeVadSources.clear();
        this.subscribers.clear();
        this.sourceBuffers.clear();
    }

    private ensureDispatchLoop(): void {
        if (this.dispatchTimerId != null) {
            return;
        }
        this.dispatchTimerId = window.setInterval(() => {
            this.emitNextFrame();
        }, this.frameSamples / this.bridgeSampleRate * 1000);
    }

    private maybeStopDispatchLoop(): void {
        if (this.subscribers.size !== 0 || this.userInputSession !== null) {
            return;
        }
        if (this.dispatchTimerId != null) {
            window.clearInterval(this.dispatchTimerId);
            this.dispatchTimerId = null;
        }
    }

    private emitNextFrame(): void {
        if (this.subscribers.size === 0) {
            return;
        }

        for (const target of this.subscribers.values()) {
            const floatFrame = new Float32Array(this.frameSamples);
            for (const [sourceId, sourceBuffer] of this.sourceBuffers.entries()) {
                if (sourceId === target.participantId) {
                    continue;
                }
                const available = Math.min(sourceBuffer.length, this.frameSamples);
                for (let index = 0; index < available; index += 1) {
                    const mixed = floatFrame[index]! + (sourceBuffer[index] ?? 0);
                    floatFrame[index] = Math.max(-1, Math.min(1, mixed));
                }
            }
            const targetFrame = (
                target.sampleRate === this.bridgeSampleRate
                    ? encodeFloat32ToPcm16(floatFrame)
                    : encodeFloat32ToPcm16(
                        resampleFloat32(floatFrame, this.bridgeSampleRate, target.sampleRate),
                    )
            );
            void Promise.resolve(target.pushAudioChunk(targetFrame));
        }
        for (const [sourceId, sourceBuffer] of this.sourceBuffers.entries()) {
            const available = Math.min(sourceBuffer.length, this.frameSamples);
            if (available > 0) {
                sourceBuffer.splice(0, available);
            }
            if (sourceBuffer.length === 0) {
                this.sourceBuffers.delete(sourceId);
            }
        }
    }

    private ensureSourceState(
        sourceId: WebBridgeParticipantId,
        {
            autoEmitVad,
            vadRedemptionMs,
        }: {
            autoEmitVad: boolean;
            vadRedemptionMs: number;
        },
    ): SourceRuntimeState {
        const existing = this.sourceStates.get(sourceId);
        if (existing) {
            existing.autoEmitVad = autoEmitVad;
            existing.vadRedemptionMs = vadRedemptionMs;
            return existing;
        }
        const created: SourceRuntimeState = {
            autoEmitVad,
            vadRedemptionMs,
            endTimerId: null,
        };
        this.sourceStates.set(sourceId, created);
        return created;
    }

    private hasAudibleVadForTarget(targetParticipantId: WebBridgeParticipantId): boolean {
        for (const sourceId of this.activeVadSources) {
            if (sourceId !== targetParticipantId) {
                return true;
            }
        }
        return false;
    }
}

/**
 * Create a web-only shared audio bridge backed by one continuous PCM stream.
 *
 * The bridge always emits a continuous stream to bridge-input sessions; when no
 * user or bot audio is present the stream consists of silence.
 *
 * @returns A new web audio bridge instance.
 */
function createWebAudioBridge(): WebAudioBridge {
    return new DefaultWebAudioBridge();
}

function resolveVadRedemptionMs(value: number | undefined): number {
    if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
        return DEFAULT_VAD_REDEMPTION_MS;
    }
    return value;
}

function mixIntoSourceBuffer(
    target: Map<WebBridgeParticipantId, number[]>,
    sourceId: WebBridgeParticipantId,
    source: Float32Array,
): void {
    const sourceBuffer = target.get(sourceId) ?? [];
    for (let index = 0; index < source.length; index += 1) {
        sourceBuffer.push(Math.max(-1, Math.min(1, source[index]!)));
    }
    target.set(sourceId, sourceBuffer);
}

function decodePcm16ToFloat32(pcmChunkInt16: ArrayBuffer): Float32Array {
    const int16 = new Int16Array(pcmChunkInt16);
    const float32 = new Float32Array(int16.length);
    for (let index = 0; index < int16.length; index += 1) {
        float32[index] = int16[index]! / 32768;
    }
    return float32;
}

function encodeFloat32ToPcm16(float32: Float32Array): ArrayBuffer {
    const int16 = new Int16Array(float32.length);
    for (let index = 0; index < float32.length; index += 1) {
        const clamped = Math.max(-1, Math.min(1, float32[index]!));
        int16[index] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;
    }
    return int16.buffer;
}

function resampleFloat32(
    input: Float32Array,
    sourceSampleRate: number,
    targetSampleRate: number,
): Float32Array {
    if (
        sourceSampleRate === targetSampleRate
        || input.length === 0
        || sourceSampleRate <= 0
        || targetSampleRate <= 0
    ) {
        return input;
    }

    const ratio = sourceSampleRate / targetSampleRate;
    const outputLength = Math.max(1, Math.floor(input.length / ratio));
    const output = new Float32Array(outputLength);

    for (let index = 0; index < outputLength; index += 1) {
        const position = index * ratio;
        const leftIndex = Math.floor(position);
        const rightIndex = Math.min(leftIndex + 1, input.length - 1);
        const fraction = position - leftIndex;
        const left = input[leftIndex] ?? 0;
        const right = input[rightIndex] ?? left;
        output[index] = left + (right - left) * fraction;
    }
    return output;
}
