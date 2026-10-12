import { ApiError } from "../api/client";

export function ErrorNote({ error }: { error: unknown }) {
  const message =
    error instanceof ApiError
      ? error.status === 403
        ? "You do not have access to this."
        : error.status === 404
          ? "Not found."
          : error.message
      : error instanceof Error
        ? error.message
        : "Something went wrong.";
  return (
    <p className="error" role="alert">
      {message}
    </p>
  );
}
