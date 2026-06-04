[**xtalk-client**](../globals.zh.md)

***

[xtalk-client](../globals.zh.md) / Session

# 接口: Session

定义于: `session/types.ts:86`

前端入口暴露的公开会话控制器。

## 属性

### muted

> **muted**: `boolean`

定义于: `session/types.ts:126`

麦克风采集链路当前是否静音。

***

### state

> `readonly` **state**: `object`

定义于: `session/types.ts:104`

当前的会话状态快照。

#### caption

> **caption**: `string`

#### connectionState

> **connectionState**: `"connected"` \| `"reconnecting"` \| `"disconnected"`

#### latency

> **latency**: `object`

##### latency.asr?

> `optional` **asr?**: `number`

##### latency.llmFirstToken?

> `optional` **llmFirstToken?**: `number`

##### latency.llmSentence?

> `optional` **llmSentence?**: `number`

##### latency.network?

> `optional` **network?**: `number`

##### latency.ttsFirstChunk?

> `optional` **ttsFirstChunk?**: `number`

#### messages

> **messages**: `ConversationMessage`[]

#### retrieval

> **retrieval**: `string`

#### sessionId

> **sessionId**: `string` \| `null`

#### streamState

> **streamState**: `"idle"` \| `"listening"` \| `"processing"` \| `"speaking"`

#### thought

> **thought**: `string`

#### tool\_call

> **tool\_call**: `object`

##### tool\_call.args

> **args**: `Record`&lt;`string`, `any`&gt;

##### tool\_call.name

> **name**: `string`

#### user

> **user**: `ConversationUser` \| `null`

## 方法

### changeVoice()

> **changeVoice**(`voiceName`): `Promise`&lt;`void`&gt;

定义于: `session/types.ts:132`

请求为后续助手语音合成切换语音。

#### 参数

##### voiceName

`string`

目标语音标识符。

#### 返回

`Promise`&lt;`void`&gt;

***

### close()

> **close**(): `Promise`&lt;`void`&gt;

定义于: `session/types.ts:94`

关闭当前运行时连接和音频资源。

#### 返回

`Promise`&lt;`void`&gt;

***

### getSessions()

> **getSessions**(): `Promise`&lt;`SessionSummary`[]&gt;

定义于: `session/types.ts:143`

获取当前用户可用的持久化会话。

#### 返回

`Promise`&lt;`SessionSummary`[]&gt;

***

### onFullAudioChunk()

> **onFullAudioChunk**(`callback`): `void`

定义于: `session/types.ts:122`

注册一个用于接收合并后的全双工 PCM 音频块的回调。

#### 参数

##### callback

`AudioChunkCallback`

完整音频监听器。

#### 返回

`void`

***

### onInputAudioChunk()

> **onInputAudioChunk**(`callback`): `void`

定义于: `session/types.ts:110`

注册一个用于接收麦克风输入 PCM 音频块的回调。

#### 参数

##### callback

`AudioChunkCallback`

输入音频监听器。

#### 返回

`void`

***

### onOutputAudioChunk()

> **onOutputAudioChunk**(`callback`): `void`

定义于: `session/types.ts:116`

注册一个用于接收扬声器输出 PCM 音频块的回调。

#### 参数

##### callback

`AudioChunkCallback`

输出音频监听器。

#### 返回

`void`

***

### onStateChange()

> **onStateChange**(`callback`): `void`

定义于: `session/types.ts:100`

注册一个在会话状态变化时触发的回调。

#### 参数

##### callback

(`state`) => `void`

状态变化监听器。

#### 返回

`void`

***

### open()

> **open**(): `Promise`&lt;`void`&gt;

定义于: `session/types.ts:90`

打开会话运行时，并在需要时执行鉴权。

#### 返回

`Promise`&lt;`void`&gt;

***

### switchSession()

> **switchSession**(`sessionId`): `Promise`&lt;`void`&gt;

定义于: `session/types.ts:149`

将当前会话切换到一个已持久化会话，或启动一个新会话。

#### 参数

##### sessionId

`string` \| `null`

目标会话标识符；传入 `null` 时启动新会话。

#### 返回

`Promise`&lt;`void`&gt;

***

### uploadFile()

> **uploadFile**(`file`, `endpoint?`): `Promise`&lt;`void`&gt;

定义于: `session/types.ts:139`

将文件上传到当前会话上下文中。

#### 参数

##### file

`Blob`

要上传的文件 Blob。

##### endpoint?

`string` \| `URL`

可选的上传端点覆盖值。

#### 返回

`Promise`&lt;`void`&gt;
