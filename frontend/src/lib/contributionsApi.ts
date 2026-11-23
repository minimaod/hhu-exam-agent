import { apiFetch } from "./apiFetch.ts";
import { readErrorDetail } from "./papersApi.ts";

export type ContributionSubmitResponse = {
  contribution_id: string;
  course: string;
  year: string;
  status: "pending_review";
  message: string;
};

export async function submitContribution(params: {
  file: File;
  course: string;
  year: string;
}): Promise<ContributionSubmitResponse> {
  const form = new FormData();
  form.append("file", params.file);
  form.append("course", params.course);
  form.append("year", params.year);

  const response = await apiFetch("/api/contributions", {
    method: "POST",
    body: form,
  });

  if (!response.ok) {
    throw new Error(await readErrorDetail(response));
  }

  return (await response.json()) as ContributionSubmitResponse;
}
