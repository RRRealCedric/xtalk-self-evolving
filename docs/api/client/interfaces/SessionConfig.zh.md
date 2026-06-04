[**xtalk-client**](../globals.zh.md)

***

[xtalk-client](../globals.zh.md) / SessionConfig

# 接口: SessionConfig

定义于: `session/types.ts:68`

创建会话时使用的配置覆盖项。

## 属性

### inputConfig?

> `optional` **inputConfig?**: `Partial`&lt;`InputAudioSessionConfig`&gt;

定义于: `session/types.ts:72`

输入音频会话覆盖项，例如采样率。

***

### outputConfig?

> `optional` **outputConfig?**: `Partial`&lt;`OutputAudioSessionConfig`&gt;

定义于: `session/types.ts:76`

输出音频会话覆盖项，例如播放采样率。

***

### serviceURLs?

> `optional` **serviceURLs?**: `Partial`&lt;\{ `login`: `ResolvableURL`; `sessionDetail`: `ResolvableURL` \| ((`sessionId`) => `ResolvableURL`); `sessions`: `ResolvableURL`; `upload`: `ResolvableURL`; \}&gt;

定义于: `session/types.ts:80`

辅助 HTTP 服务端点的可选覆盖项。
