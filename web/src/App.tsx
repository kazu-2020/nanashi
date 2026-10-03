import { useEffect, useState } from "react";
import { Chip, Link, Spinner } from "@heroui/react";
import { type Model } from "./gen/nanashi/v1/model_pb";
import { modelClient } from "./client";
import Dimensions from "./Dimensions";

function modelIdOf(hash: string): string | undefined {
  const m = /^#\/models\/(.+)$/.exec(hash);
  return m ? decodeURIComponent(m[1]) : undefined;
}

export default function App() {
  const [hash, setHash] = useState(location.hash);
  useEffect(() => {
    const onChange = () => setHash(location.hash);
    addEventListener("hashchange", onChange);
    return () => removeEventListener("hashchange", onChange);
  }, []);
  const modelId = modelIdOf(hash);

  return (
    <main className="mx-auto max-w-6xl p-8">
      {modelId === undefined ? (
        <ModelList />
      ) : (
        <>
          <Link href="#/">← モデル</Link>
          <h1 className="my-4 text-2xl font-bold">{modelId}</h1>
          <Dimensions key={modelId} modelId={modelId} />
        </>
      )}
    </main>
  );
}

function ModelList() {
  const [models, setModels] = useState<Model[]>();
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    modelClient.listModels({}).then(
      (r) => setModels(r.models),
      () => setFailed(true),
    );
  }, []);

  return (
    <>
      <h1 className="mb-4 text-2xl font-bold">モデル</h1>
      {failed ? (
        <p className="text-danger">モデルの一覧を読めません。</p>
      ) : !models ? (
        <Spinner />
      ) : models.length === 0 ? (
        <p>モデルがありません。</p>
      ) : (
        <ul className="flex flex-col gap-2">
          {models.map((m) => (
            <li key={m.id} className="flex items-center gap-2">
              <Link href={`#/models/${encodeURIComponent(m.id)}`}>{m.id}</Link>
              {m.open && <Chip color="success">開いています</Chip>}
            </li>
          ))}
        </ul>
      )}
    </>
  );
}
