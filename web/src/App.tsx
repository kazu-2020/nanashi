import { useEffect, useState } from "react";
import { Chip, Spinner } from "@heroui/react";
import { createClient } from "@connectrpc/connect";
import { createConnectTransport } from "@connectrpc/connect-web";
import { ModelService, type Model } from "./gen/nanashi/v1/model_pb";

const client = createClient(ModelService, createConnectTransport({ baseUrl: "/" }));

export default function App() {
  const [models, setModels] = useState<Model[]>();
  const [error, setError] = useState<string>();
  useEffect(() => {
    client.listModels({}).then(
      (r) => setModels(r.models),
      (e: Error) => setError(e.message),
    );
  }, []);

  return (
    <main className="mx-auto max-w-2xl p-8">
      <h1 className="mb-4 text-2xl font-bold">モデル</h1>
      {error ? (
        <p className="text-danger">モデルの一覧を読めません: {error}</p>
      ) : !models ? (
        <Spinner />
      ) : models.length === 0 ? (
        <p>モデルがありません。</p>
      ) : (
        <ul className="flex flex-col gap-2">
          {models.map((m) => (
            <li key={m.id} className="flex items-center gap-2">
              {m.id}
              {m.open && <Chip color="success">開いています</Chip>}
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}
