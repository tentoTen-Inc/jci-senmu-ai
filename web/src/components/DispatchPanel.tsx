// 会議の「案内・配信」（docs/meeting-notice-design.md §7.1・段階 A: LINE）
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { Badge, Button, ErrorNote, Field, Loading, Panel, fmtDateTime, useUi } from "./ui";

type Sent = {
  template_id: string | null;
  text: string | null;
  group_name: string | null;
  recipients: number;
  sent_at: string | null;
  sent_by: string | null;
};
type DispatchView = {
  dispatch: { agenda_url: string | null; opinion_url: string | null; line: Sent; history: Sent[] };
  templates: { template_id: string; name: string; purpose: string; mention: string }[];
  template_id: string | null;
  mention: string;
  preview: string | null;
  error: string | null;
  missing: string[];
  late: boolean;
  deliver_deadline: string;
  proposals: { no: string; title: string }[];
  destination: { mode: "group" | "individual"; group_id: string | null; group_name: string | null; targets: number; linked: number };
};

/** 締切は「N日前の24:00」なので、0:00 ちょうどは前の日の 24:00 と書く */
function deadlineText(iso: string): string {
  const d = new Date(iso);
  if (d.getHours() !== 0 || d.getMinutes() !== 0) return fmtDateTime(iso);
  const prev = new Date(d.getTime() - 24 * 3600 * 1000);
  return `${fmtDateTime(prev.toISOString(), false)} 24:00`;
}

export default function DispatchPanel({ eventId }: { eventId: string }) {
  const { confirm, toast } = useUi();
  const [view, setView] = useState<DispatchView | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [templateId, setTemplateId] = useState<string | null>(null);
  const [urls, setUrls] = useState({ agenda_url: "", opinion_url: "" });
  const [busy, setBusy] = useState(false);

  const load = useCallback(
    async (tid?: string | null) => {
      try {
        const q = tid ? `?template_id=${encodeURIComponent(tid)}` : "";
        const v = await api<DispatchView>(`/events/${eventId}/dispatch${q}`);
        setView(v);
        setTemplateId(v.template_id);
        setUrls({ agenda_url: v.dispatch.agenda_url ?? "", opinion_url: v.dispatch.opinion_url ?? "" });
        setErr(null);
      } catch (e) {
        setErr((e as Error).message);
      }
    },
    [eventId]
  );
  useEffect(() => {
    load();
  }, [load]);

  async function saveUrls() {
    setBusy(true);
    try {
      const v = await api<DispatchView>(`/events/${eventId}/dispatch`, { method: "PUT", body: JSON.stringify(urls) });
      setView(v);
      toast("リンクを保存しました");
      if (templateId && templateId !== v.template_id) load(templateId);
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(false);
    }
  }

  async function send() {
    if (!view || !templateId || !view.preview) return;
    const d = view.destination;
    const where =
      d.mode === "group"
        ? `LINE グループ「${d.group_name}」`
        : `LINE 連携済みの対象者 ${d.linked}人（グループがないため個別に）`;
    const ok = await confirm({
      title: "LINE に送りますか？",
      body: (
        <>
          <p>
            送り先: <b>{where}</b>
            {view.mention === "all" && d.mode === "group" && "（全員にメンション）"}
          </p>
          {view.dispatch.line.sent_at && (
            <p className="mt-1 text-danger">このテンプレートで {fmtDateTime(view.dispatch.line.sent_at)} に送っています。もう一度送ります。</p>
          )}
          <p className="mt-1">グループへの投稿は取り消せません。</p>
        </>
      ),
      confirmLabel: "送る",
    });
    if (!ok) return;
    setBusy(true);
    try {
      const v = await api<DispatchView>(`/events/${eventId}/dispatch/line`, {
        method: "POST",
        body: JSON.stringify({ template_id: templateId, expected_group_id: d.group_id }),
      });
      setView(v);
      toast("LINE に送りました");
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(false);
    }
  }

  if (err) return <Panel title="案内・配信"><ErrorNote>{err}</ErrorNote></Panel>;
  if (!view) return <Panel title="案内・配信"><Loading /></Panel>;
  const d = view.destination;
  const dirty = urls.agenda_url !== (view.dispatch.agenda_url ?? "") || urls.opinion_url !== (view.dispatch.opinion_url ?? "");

  return (
    <Panel
      title="案内・配信"
      action={<Link className="text-line-ink underline" to="/notice-templates">テンプレートを直す</Link>}
    >
      <div className="mb-3 flex flex-wrap items-center gap-2 text-sm">
        <span className="text-ink-sub">資料配信の締切: <span className="num">{deadlineText(view.deliver_deadline)}</span></span>
        {view.late && <Badge tone="amber">締切を過ぎています（お詫びの一文が入ります）</Badge>}
        {view.dispatch.line.sent_at && <Badge tone="green">LINE 送信済み {fmtDateTime(view.dispatch.line.sent_at)}</Badge>}
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="アジェンダの URL" hint="Google ドキュメントのリンク（共有は「リンクを知っている全員」に）">
          <input className="w-full" placeholder="https://docs.google.com/document/d/…" value={urls.agenda_url}
            onChange={(e) => setUrls({ ...urls, agenda_url: e.target.value })} />
        </Field>
        <Field label="意見書の URL（任意）" hint="その回のシートのリンク">
          <input className="w-full" placeholder="https://docs.google.com/spreadsheets/d/…" value={urls.opinion_url}
            onChange={(e) => setUrls({ ...urls, opinion_url: e.target.value })} />
        </Field>
      </div>
      {dirty && (
        <div className="mt-2 flex justify-end">
          <Button size="sm" onClick={saveUrls} disabled={busy}>リンクを保存</Button>
        </div>
      )}

      <div className="mt-4 grid gap-4 sm:grid-cols-2">
        <div>
          <Field label="LINE のテンプレート">
            <select className="w-full" value={templateId ?? ""} onChange={(e) => load(e.target.value)}>
              {view.templates.map((t) => (
                <option key={t.template_id} value={t.template_id}>{t.name}</option>
              ))}
            </select>
          </Field>
          <div className="mt-3 rounded-2xl bg-gray-50 px-4 py-3 text-sm">
            <div className="text-xs text-ink-sub">送り先</div>
            {d.mode === "group" ? (
              <div className="font-bold">LINE グループ「{d.group_name}」<span className="ml-1 font-normal text-ink-sub">（対象 {d.targets}人）</span></div>
            ) : (
              <div className="font-bold">
                個別に {d.linked}人<span className="ml-1 font-normal text-ink-sub">（対象 {d.targets}人のうち LINE 連携済み。会議体のグループは設定にありません）</span>
              </div>
            )}
            {view.mention === "all" && d.mode === "group" && <div className="mt-1 text-xs text-ink-sub">先頭で全員にメンションします</div>}
          </div>
          {view.missing.length > 0 && (
            <ul className="mt-3 list-disc pl-5 text-xs text-hold">
              {view.missing.map((m) => <li key={m}>{m}</li>)}
            </ul>
          )}
          <div className="mt-3 text-xs text-ink-sub">
            上程議案 {view.proposals.length}件
            {view.proposals.length > 0 && `（${view.proposals.map((p) => p.title).join("、")}）`}
          </div>
        </div>
        <div>
          <div className="mb-1 text-xs font-bold text-ink-sub">プレビュー</div>
          {view.error ? (
            <ErrorNote>{view.error}</ErrorNote>
          ) : (
            <div className="whitespace-pre-wrap break-all rounded-2xl rounded-tl-sm bg-line-soft px-4 py-3 text-sm leading-relaxed">
              {view.mention === "all" && d.mode === "group" && <span className="font-bold text-line-ink">@All </span>}
              {view.preview}
            </div>
          )}
        </div>
      </div>

      <div className="mt-4 flex justify-end">
        <Button variant="primary" onClick={send} disabled={busy || dirty || !view.preview || !templateId}>
          {busy ? "送信中…" : "LINE に送る"}
        </Button>
      </div>
      {dirty && <p className="mt-1 text-right text-xs text-ink-sub">リンクを保存してから送ってください。</p>}

      {view.dispatch.history.length > 0 && (
        <details className="mt-4 text-sm">
          <summary className="cursor-pointer font-bold text-ink-sub">送った記録（{view.dispatch.history.length}件）</summary>
          <ul className="mt-2 space-y-2">
            {[...view.dispatch.history].reverse().map((h, i) => (
              <li key={i} className="rounded-xl bg-gray-50 px-3 py-2">
                <div className="num text-xs text-ink-sub">
                  {fmtDateTime(h.sent_at)}・{h.group_name ? `「${h.group_name}」` : `個別 ${h.recipients}人`}・{h.sent_by}
                </div>
                <div className="mt-1 whitespace-pre-wrap break-all text-xs">{h.text}</div>
              </li>
            ))}
          </ul>
        </details>
      )}
    </Panel>
  );
}
