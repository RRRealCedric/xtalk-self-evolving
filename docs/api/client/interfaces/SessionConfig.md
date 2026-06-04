[**xtalk-client**](../globals.md)

***

[xtalk-client](../globals.md) / SessionConfig

# Interface: SessionConfig

Defined in: session/types.ts:68

Configuration overrides used when creating a session.

## Properties

### inputConfig?

> `optional` **inputConfig?**: `Partial`&lt;`InputAudioSessionConfig`&gt;

Defined in: session/types.ts:72

Input audio session overrides such as capture sample rate.

***

### outputConfig?

> `optional` **outputConfig?**: `Partial`&lt;`OutputAudioSessionConfig`&gt;

Defined in: session/types.ts:76

Output audio session overrides such as playback sample rate.

***

### serviceURLs?

> `optional` **serviceURLs?**: `Partial`&lt;\{ `login`: `ResolvableURL`; `sessionDetail`: `ResolvableURL` \| ((`sessionId`) => `ResolvableURL`); `sessions`: `ResolvableURL`; `upload`: `ResolvableURL`; \}&gt;

Defined in: session/types.ts:80

Optional overrides for auxiliary HTTP service endpoints.
