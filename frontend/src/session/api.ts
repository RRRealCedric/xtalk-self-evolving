import { BaseHTTPClient } from "../bases/http";
import type { ResolvableURL, SessionServiceURLs } from "../bases/http";
import { Conversation } from "../conversation";
import type { ConversationMessage } from "../conversation";
import type { SessionDetail, SessionSummary } from "./types";

export { createSessionAPI };

type AuthorizedOperation = <T>(operation: (token: string) => Promise<T>) => Promise<T>;

function mapSessionMessages(messages: SessionDetail["messages"]): ConversationMessage[] {
    return messages.map((message) => ({
        role: message.role,
        content: message.content,
        final: true,
    }));
}

function createSessionAPI(
    {
        closeRuntime,
        conversation,
        ensureLoggedIn,
        httpClient,
        serviceURLs,
        withAuthorizedToken,
    }: {
        closeRuntime: () => Promise<void>;
        conversation: Conversation;
        ensureLoggedIn: () => Promise<void>;
        httpClient: BaseHTTPClient;
        serviceURLs: SessionServiceURLs;
        withAuthorizedToken: AuthorizedOperation;
    },
) {
    async function authorizedGetJSON<T>(input: ResolvableURL): Promise<T> {
        return await withAuthorizedToken((token) => httpClient.getJSON<T>(input, token));
    }

    async function loadSessionDetail(sessionId: string): Promise<SessionDetail> {
        return await authorizedGetJSON<SessionDetail>(serviceURLs.sessionDetail(sessionId));
    }

    return {
        async getSessions(): Promise<SessionSummary[]> {
            const payload = await authorizedGetJSON<{ sessions?: SessionSummary[] }>(
                serviceURLs.sessions,
            );
            return payload.sessions ?? [];
        },
        async switchSession(sessionId: string | null): Promise<void> {
            await ensureLoggedIn();
            await closeRuntime();
            if (!sessionId) {
                conversation.switch(null, []);
                return;
            }

            const payload = await loadSessionDetail(sessionId);
            conversation.switch(payload.session_id, mapSessionMessages(payload.messages));
        },
        async probeSessionRecovery(sessionId: string | null): Promise<boolean> {
            if (!sessionId) {
                return false;
            }
            try {
                await loadSessionDetail(sessionId);
                return true;
            } catch {
                return false;
            }
        },
        async refreshSession(sessionId: string): Promise<void> {
            const payload = await loadSessionDetail(sessionId);
            conversation.switch(payload.session_id, mapSessionMessages(payload.messages));
        },
        async uploadFile(file: Blob, endpoint?: string | URL): Promise<void> {
            const sessionId = conversation.state.sessionId;
            if (!sessionId) {
                throw new Error("No session selected");
            }

            conversation.state.streamState = "processing";
            try {
                await withAuthorizedToken((token) =>
                    httpClient.postFile(endpoint ?? serviceURLs.upload, token, sessionId, file),
                );
            } finally {
                conversation.state.streamState = "idle";
            }
        },
    };
}
