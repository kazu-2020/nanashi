import { createRootRoute, Outlet } from "@tanstack/react-router";
import { NuqsAdapter } from "nuqs/adapters/tanstack-router";

// The nuqs adapter reads the router, so it goes inside the root route.
export const Route = createRootRoute({
  component: () => (
    <NuqsAdapter>
      <main className="p-6">
        <h1 className="text-xl font-bold">nanashi</h1>
        <Outlet />
      </main>
    </NuqsAdapter>
  ),
});
