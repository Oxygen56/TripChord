export type Money = { amount: string; currency: string };

export type PlanItem = {
  id: string;
  kind: "transport" | "lodging" | "activity" | "meal" | "buffer";
  title: string;
  starts_at: string;
  ends_at: string;
  location_name: string | null;
  cost: Money | null;
  source_refs: string[];
  locked: boolean;
};

export type PlanVersion = {
  id: string;
  trip_id: string;
  version: number;
  status: string;
  items: PlanItem[];
  parent_version_id: string | null;
  applied_event_ids: string[];
};

export type TripSpec = {
  origin: string;
  destinations: string[];
  start_date: string;
  end_date: string;
  budget?: Money;
  max_main_activities_per_day: number;
  interests: string[];
  must_visit: string[];
};

export type Workspace = {
  id: string;
  title: string;
  spec: TripSpec;
  plans: PlanVersion[];
  events: Array<{ event: { id: string; kind: string }; result: unknown }>;
};

export type Job = {
  id: string;
  workspace_id: string;
  status: "queued" | "running" | "succeeded" | "failed";
  stage: string;
  progress: number;
  error: string | null;
};

export type Offer = {
  id: string;
  kind: "flight" | "rail" | "lodging" | "activity";
  title: string;
  price_state: string;
  price: { total: Money };
  source: { provider: string; mode: string; captured_at: string };
  terms: { refundable: boolean | null; cancellation_summary: string | null };
};

export type PlanDiff = {
  added_item_ids: string[];
  removed_item_ids: string[];
  changed_items: Array<{ item_id: string; changed_fields: string[] }>;
};

export type ReplanResult = {
  status: "ready" | "blocked" | "no_effect";
  message: string;
  overall_preservation_ratio: number;
  unaffected_preservation_ratio: number;
  diff: PlanDiff;
  preference: "minimum_change" | "balanced" | "quality_first";
  selected_mode: "local" | "global";
  candidates: Array<{
    mode: "local" | "global";
    hard_valid: boolean;
    preservation_ratio: number;
    utility_retention: number;
  }>;
};

type StartPlanningResponse = {
  workspace: Workspace;
  job: Job;
  data_mode: string;
  candidate_count: number;
};

let apiCredential = sessionStorage.getItem("tripchord-api-key") ?? "";

export function setApiCredential(credential: string): void {
  apiCredential = credential.trim();
  if (apiCredential) sessionStorage.setItem("tripchord-api-key", apiCredential);
  else sessionStorage.removeItem("tripchord-api-key");
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(apiCredential ? { Authorization: `Bearer ${apiCredential}` } : {}),
      ...init?.headers,
    },
  });
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as { detail?: string } | null;
    throw new Error(body?.detail ?? `请求失败 (${response.status})`);
  }
  return response.json() as Promise<T>;
}

export function startPlanning(spec: TripSpec): Promise<StartPlanningResponse> {
  return request("/api/v1/trips/plan", {
    method: "POST",
    body: JSON.stringify({ spec }),
  });
}

export function loadWorkspace(workspaceId: string): Promise<Workspace> {
  return request(`/api/v1/workspaces/${workspaceId}`);
}

export function comparePlans(
  workspaceId: string,
  fromVersion: number,
  toVersion: number,
): Promise<PlanDiff> {
  return request(
    `/api/v1/workspaces/${workspaceId}/plans/${fromVersion}/diff/${toVersion}`,
  );
}

export async function searchOffers(spec: TripSpec): Promise<Offer[]> {
  const common = {
    origin: spec.origin,
    destination: spec.destinations[0],
    start_date: spec.start_date,
    end_date: spec.end_date,
  };
  const [flights, lodging] = await Promise.all([
    request<{ offers: Offer[] }>("/api/v1/offers/search", {
      method: "POST",
      body: JSON.stringify({ ...common, kind: "flight" }),
    }),
    request<{ offers: Offer[] }>("/api/v1/offers/search", {
      method: "POST",
      body: JSON.stringify({ ...common, kind: "lodging" }),
    }),
  ]);
  return [...flights.offers, ...lodging.offers];
}

export function replanWorkspace(
  workspaceId: string,
  input: {
    targetId: string;
    kind: string;
    preference: "minimum_change" | "balanced" | "quality_first";
    payload?: Record<string, string | number>;
  },
): Promise<{ result: ReplanResult; workspace: Workspace }> {
  return request(`/api/v1/workspaces/${workspaceId}/events/replan`, {
    method: "POST",
    body: JSON.stringify({
      preference: input.preference,
      event: {
        id: `ui-${input.kind}-${crypto.randomUUID()}`,
        trip_id: workspaceId,
        kind: input.kind,
        occurred_at: new Date().toISOString(),
        target_refs: [input.targetId],
        payload: input.payload ?? {},
      },
    }),
  });
}

export function subscribeToJob(
  workspaceId: string,
  jobId: string,
  onJob: (job: Job) => void,
  onError: (message: string) => void,
): () => void {
  let active = true;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const poll = async () => {
    try {
      const job = await request<Job>(
        `/api/v1/workspaces/${workspaceId}/jobs/${jobId}`,
      );
      if (!active) return;
      onJob(job);
      if (job.status !== "succeeded" && job.status !== "failed") {
        timer = setTimeout(poll, 300);
      }
    } catch (caught) {
      if (!active) return;
      onError(caught instanceof Error ? caught.message : "进度查询中断，请稍后重试");
    }
  };
  void poll();
  return () => {
    active = false;
    if (timer) clearTimeout(timer);
  };
}
