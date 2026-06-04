/**
 * @packageDocumentation
 *
 * Public frontend client APIs for creating and controlling X-Talk sessions.
 */

import { createSession } from "./session/create";
import { createAudioBridge } from "./platforms/index";

export { createSession };
export { createAudioBridge };
export type {
    Session,
    SessionConfig,
} from "./session/types";
