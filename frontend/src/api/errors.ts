// The error-code taxonomy the frontend actually reacts to — a hand-written
// subset of the gateway's codes. Adding a code here is a statement that some
// component branches on it; codes the server invents later still flow through
// ApiError.code as plain strings and land in each component's default branch.

import { ApiError } from "./client";

export type ErrorCode =
  | "AUTH_TOKEN_EXPIRED"
  | "AUTH_INVALID_CREDENTIALS"
  | "PRICE_CHANGED"
  | "ITEM_UNAVAILABLE"
  | "RESTAURANT_CLOSED"
  | "VALIDATION_FAILED"
  | "IDEMPOTENCY_IN_PROGRESS"
  | "ORDER_NOT_CANCELLABLE"
  | "ORDER_ALREADY_DECIDED"
  | "ORDER_STATE_CONFLICT"
  | "DEPENDENCY_UNAVAILABLE"
  | "NOT_FOUND";

/**
 * Narrowing guard replacing every `instanceof ApiError && .code === "…"` pair:
 * the code argument is spell-checked against the union, and the caller gets a
 * typed ApiError (details, status, requestId) in the guarded branch.
 */
export function hasCode(error: unknown, code: ErrorCode): error is ApiError {
  return error instanceof ApiError && error.code === code;
}
