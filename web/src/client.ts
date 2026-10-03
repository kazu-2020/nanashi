import { createClient } from "@connectrpc/connect";
import { createConnectTransport } from "@connectrpc/connect-web";
import { DimensionService } from "./gen/nanashi/v1/dimension_pb";
import { ModelService } from "./gen/nanashi/v1/model_pb";

const transport = createConnectTransport({ baseUrl: "/" });

export const modelClient = createClient(ModelService, transport);
export const dimensionClient = createClient(DimensionService, transport);
