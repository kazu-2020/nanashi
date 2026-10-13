import { ConnectError, createClient, type Interceptor } from "@connectrpc/connect";
import { createConnectTransport } from "@connectrpc/connect-web";
import { PlanService } from "./gen/nanashi/v1/plan_pb";
import { uuidv7 } from "./logic";

const USER_KEY = "nanashi-user";

export const getUser = () => localStorage.getItem(USER_KEY) ?? "";
export const setUser = (u: string) =>
  u ? localStorage.setItem(USER_KEY, u) : localStorage.removeItem(USER_KEY);

// ponytail: the header names the user without a password (local development only).
// HTTP headers take only ASCII, so the login screen accepts only ASCII names.
// Production puts an authenticating proxy in front of the api.
const withUser: Interceptor = (next) => (req) => {
  req.header.set("X-Nanashi-User", getUser());
  return next(req);
};

export const api = createClient(
  PlanService,
  createConnectTransport({ baseUrl: "/", interceptors: [withUser] }),
);

export const newId = () => uuidv7(Date.now(), crypto.getRandomValues(new Uint8Array(16)));

// errorText gives the message for the user. The api sends Japanese messages.
export const errorText = (e: unknown) =>
  e instanceof ConnectError ? e.rawMessage : e instanceof Error ? e.message : String(e);
