/** A failed Control API call. Its message ends with the request ID so support can find the call. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly requestId?: string,
  ) {
    super(requestId ? `${message} (request ID ${requestId})` : message);
    this.name = "ApiError";
  }
}

export class ContractViolation extends Error {
  readonly path: string;
  constructor(
    readonly method: string,
    path: string,
    readonly status: number,
  ) {
    const pathname = new URL(path, "http://control.invalid").pathname;
    super(`Invalid Control API contract: ${method} ${pathname} (${status})`);
    this.path = pathname;
    this.name = "ContractViolation";
  }
}
