/** A failed Control API call. Its message ends with the request ID so support can find the call. */
export class ApiError extends Error {
  readonly status: number;
  readonly requestId?: string | undefined;
  constructor(status: number, message: string, requestId?: string) {
    super(requestId ? `${message} (request ID ${requestId})` : message);
    this.status = status;
    this.requestId = requestId;
    this.name = "ApiError";
  }
}

export class ContractViolation extends Error {
  readonly path: string;
  readonly method: string;
  readonly status: number;
  constructor(method: string, path: string, status: number) {
    const pathname = new URL(path, "http://control.invalid").pathname;
    super(`Invalid Control API contract: ${method} ${pathname} (${status})`);
    this.method = method;
    this.status = status;
    this.path = pathname;
    this.name = "ContractViolation";
  }
}
