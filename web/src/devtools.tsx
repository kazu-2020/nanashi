import { TanStackDevtools } from "@tanstack/react-devtools";
import { formDevtoolsPlugin } from "@tanstack/react-form-devtools";
import { ReactQueryDevtoolsPanel } from "@tanstack/react-query-devtools";
import type { AnyRouter } from "@tanstack/react-router";
import { TanStackRouterDevtoolsPanel } from "@tanstack/react-router-devtools";
// nuqs has no devtools panel. This logs its state changes to the console if localStorage.debug contains "nuqs".
import "nuqs/debug";

// main.tsx loads this file only in development, so the production build does not include the devtools.
export default function Devtools({ router }: { router: AnyRouter }) {
  return (
    <TanStackDevtools
      plugins={[
        { name: "TanStack Query", render: <ReactQueryDevtoolsPanel /> },
        { name: "TanStack Router", render: <TanStackRouterDevtoolsPanel router={router} /> },
        formDevtoolsPlugin(),
      ]}
    />
  );
}
