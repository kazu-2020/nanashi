import { createRootRoute, Outlet } from "@tanstack/react-router";

export const Route = createRootRoute({
  component: () => (
    <main className="p-6">
      <h1 className="text-xl font-bold">nanashi</h1>
      <Outlet />
    </main>
  ),
});
