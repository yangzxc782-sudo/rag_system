import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Keep the isolated PDF mock build away from the developer's running build.
  distDir: process.env.PDF_STRUCTURE_E2E_MOCK === "1" ? ".next-pdf-structure" : ".next",
};

export default nextConfig;
