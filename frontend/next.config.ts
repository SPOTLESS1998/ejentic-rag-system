import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Build a self-contained server bundle at `.next/standalone`, so the production
  // image can run `node server.js` without installing node_modules at all. This is
  // the documented approach for Docker in this Next version (see
  // node_modules/next/dist/docs/01-app/03-api-reference/05-config/01-next-config-js/output.md).
  //
  // Two things this buys the deployment, beyond image size:
  //   • The container runs the code that was BUILT, not whatever is on the host
  //     disk. The dev compose file bind-mounts ./frontend into the container,
  //     which is convenient locally and exactly the "is the running process the
  //     code I fixed?" trap that already cost this project a day.
  //   • `next start` needs the full dependency tree present at runtime; the
  //     standalone server does not. Fewer files in the runtime image, none of them
  //     dev dependencies.
  //
  // Note: server.js does NOT serve `public/` or `.next/static` on its own — the
  // Dockerfile copies both into the standalone tree. Dropping that copy produces a
  // site that loads but has no CSS, which reads as a broken deploy rather than a
  // missing step.
  output: "standalone",
};

export default nextConfig;
