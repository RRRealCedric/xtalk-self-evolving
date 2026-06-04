import { BasePersistenceStore } from "../bases/persistence";
import type { ConversationMessage, ConversationUser } from "../conversation";

export type { PersistedConversationSnapshot };
export {
    buildPersistedConversationSnapshot,
    clearPersistedConversationSnapshot,
    loadPersistedConversationSnapshot,
    savePersistedConversationSnapshot,
};

type PersistedConversationSnapshot = {
    accessToken: string | null;
    supportsSessionRecovery: boolean;
    user: ConversationUser | null;
    sessionId: string | null;
    messages: ConversationMessage[];
};

function normalizePersistedMessageRole(
    value: unknown,
): ConversationMessage["role"] | null {
    return value === "user" || value === "assistant" || value === "info"
        ? value
        : null;
}

function normalizePersistedMessages(value: unknown): ConversationMessage[] {
    if (!Array.isArray(value)) {
        return [];
    }
    const messages: ConversationMessage[] = [];
    for (const item of value) {
        if (!item || typeof item !== "object") {
            continue;
        }
        const role = normalizePersistedMessageRole((item as { role?: unknown }).role);
        const content = (item as { content?: unknown }).content;
        const final = (item as { final?: unknown }).final;
        if (!role || typeof content !== "string") {
            continue;
        }
        const message: ConversationMessage = { role, content };
        if (typeof final === "boolean") {
            message.final = final;
        } else if (role !== "info") {
            message.final = true;
        }
        messages.push(message);
    }
    return messages;
}

function loadPersistedConversationSnapshot(
    persistenceStore: BasePersistenceStore,
    persistenceKey: string | null,
): PersistedConversationSnapshot | null {
    if (!persistenceKey) {
        return null;
    }
    try {
        const raw = persistenceStore.load(persistenceKey);
        if (!raw) {
            return null;
        }
        const parsed = JSON.parse(raw) as {
            accessToken?: unknown;
            supportsSessionRecovery?: unknown;
            user?: unknown;
            sessionId?: unknown;
            messages?: unknown;
        };
        const userValue = parsed.user;
        const user = userValue
            && typeof userValue === "object"
            && typeof (userValue as { id?: unknown }).id === "string"
            ? { id: (userValue as { id: string }).id }
            : null;
        return {
            accessToken: typeof parsed.accessToken === "string" ? parsed.accessToken : null,
            supportsSessionRecovery: parsed.supportsSessionRecovery === true,
            user,
            sessionId: typeof parsed.sessionId === "string" ? parsed.sessionId : null,
            messages: normalizePersistedMessages(parsed.messages),
        };
    } catch {
        return null;
    }
}

function savePersistedConversationSnapshot(
    persistenceStore: BasePersistenceStore,
    persistenceKey: string | null,
    snapshot: PersistedConversationSnapshot,
): void {
    if (!persistenceKey) {
        return;
    }
    try {
        persistenceStore.save(persistenceKey, JSON.stringify(snapshot));
    } catch {
        // Ignore serialization failures so realtime usage continues normally.
    }
}

function clearPersistedConversationSnapshot(
    persistenceStore: BasePersistenceStore,
    persistenceKey: string | null,
): void {
    if (!persistenceKey) {
        return;
    }
    persistenceStore.clear(persistenceKey);
}

function buildPersistedConversationSnapshot(
    accessToken: string | null,
    supportsSessionRecovery: boolean,
    user: ConversationUser | null,
    sessionId: string | null,
    messages: ConversationMessage[],
): PersistedConversationSnapshot {
    return {
        accessToken,
        supportsSessionRecovery,
        user,
        sessionId,
        messages: messages.map((message) => ({
            role: message.role,
            content: message.content,
            ...(typeof message.final === "boolean" ? { final: message.final } : {}),
        })),
    };
}
