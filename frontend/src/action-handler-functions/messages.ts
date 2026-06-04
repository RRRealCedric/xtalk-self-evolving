import type { ActionToFunctionMap } from "./types";
const messagesMap: ActionToFunctionMap = {
    "update_asr": async (data, websocket, conversation, outputAudioSession) => {
        conversation.appendMessage({
            role: "user",
            content: data.text,
            final: false
        })
    },
    "finish_asr": async (data, websocket, conversation, outputAudioSession) => {
        conversation.appendMessage({
            role: "user",
            content: data.text,
            final: true
        })
        conversation.state.streamState = 'processing';
    },
    "update_resp": async (data, websocket, conversation, outputAudioSession) => {
        conversation.appendMessage({
            role: "assistant",
            content: data.text,
            final: false
        })
    },
    "finish_resp": async (data, websocket, conversation, outputAudioSession) => {
        conversation.appendMessage({
            role: "assistant",
            content: data.text,
            final: true
        })
    },
};

export default messagesMap;
