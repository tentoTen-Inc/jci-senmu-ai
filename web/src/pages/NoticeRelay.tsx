import { useEffect, useState, type ReactNode } from "react";
import { api } from "../api/client";
import { Badge, Button, EmptyState, Field, Loading, Panel, Segmented, fmtDateTime, useUi } from "../components/ui";

// 外部グループからの周知の転送（docs/line-relay-design.md §5）
// LINE グループから取り込んだ連絡の、AI の判定・専務の確認の状況と、管理画面からの転送。

type Tone = "green" | "gray" | "red" | "amber" | "blue";

export type RelayInfo = {
  group_id: string;
  kind: string | null;
  summary: string | null;
  deadline: string | null;
  card_sent_at: string | null;
  approved_at: string | null;
  sent_at: string | null;
  expired_at: string | null;
  post_id: string | null;
};

type TargetGroup = { key: string; label: string; group_id: string; group_name: string };
type RelayView = {
  label: string;
  text: string;
  default_text: string;
  edited: boolean;
  groups: TargetGroup[];
  kind: string | null;
  summary: string | null;
  deadline: string | null;
  card_sent_at: string | null;
  approved_at: string | null;
  approved_by: string | null;
  sent_at: string | null;
  expired_at: string | null;
  target_group_name: string | null;
  post: { status: string; send_at: string; sent_at: string | null; error: string | null } | null;
};
type SendResult = { result: "sent" | "scheduled" | "held" | "failed"; send_at: string; group_name: string; relay: RelayView };

export const KIND_LABELS: Record<string, string> = {
  share: "会員への周知",
  officer: "役員あて",
  other: "対象外",
};

/** 一覧・詳細に出す転送の状況。 */
export function relayState(r: RelayInfo, status: string): { label: string; tone: Tone } {
  if (r.sent_at) return { label: "転送済み", tone: "green" };
  if (r.post_id) return { label: "送信待ち", tone: "blue" };
  if (status === "archived") return { label: "送らない", tone: "gray" };
  if (r.expired_at) return { label: "期限切れ", tone: "gray" };
  if (!r.kind) return { label: "AI が判定中", tone: "gray" };
  if (r.kind === "share") return r.card_sent_at ? { label: "専務の確認待ち", tone: "amber" } : { label: "確認カードの送信待ち", tone: "amber" };
  if (r.kind === "officer") return { label: r.card_sent_at ? "役員あて（お知らせ済み）" : "役員あて", tone: "blue" };
  return { label: "対象外", tone: "gray" };
}

const errorText = (e: unknown) => {
  const m = e instanceof Error ? e.message : String(e);
  if (/force=true/.test(m)) return "already";
  return m;
};

export function RelayPanels({
  noticeId,
  status,
  original,
  onChanged,
}: {
  noticeId: string;
  status: string;
  original: ReactNode;
  onChanged: () => void;
}) {
  const { confirm, toast } = useUi();
  const [view, setView] = useState<RelayView | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [groupKey, setGroupKey] = useState("all");
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);

  function apply(v: RelayView) {
    setView(v);
    setText(v.text);
    if (!v.groups.some((g) => g.key === groupKey)) setGroupKey(v.groups[0]?.key ?? "all");
  }

  useEffect(() => {
    api<RelayView>(`/notices/${noticeId}/relay`).then(apply).catch((e) => setErr(e.message));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [noticeId]);

  if (err || !view)
    return (
      <div className="grid gap-4 lg:grid-cols-2">
        {original}
        <Panel title="AI の判定と専務の確認">
          {err ? <EmptyState title="読み込めませんでした">{err}</EmptyState> : <Loading />}
        </Panel>
      </div>
    );

  const group = view.groups.find((g) => g.key === groupKey);
  const state = relayState({ ...view, group_id: "", post_id: view.post ? "x" : null }, status);
  const done = !!view.sent_at || view.post?.status === "pending";

  async function send(force = false) {
    if (!group) return;
    const ok = await confirm({
      title: force ? `もう一度「${group.group_name}」に送りますか？` : `「${group.group_name}」に @All で送りますか？`,
      body: (
        <>
          <div className="max-h-48 overflow-y-auto whitespace-pre-wrap rounded-xl bg-gray-50 p-2 text-xs">@All{"\n"}{text}</div>
          <div className="mt-2 text-xs">グループの全員に通知が届きます。送ったメッセージは取り消せません。</div>
          {force && <div className="mt-1 text-xs text-danger">この連絡はすでに転送しています。同じグループにもう一度届くことがあります。</div>}
        </>
      ),
      confirmLabel: force ? "もう一度送る" : "送信する",
    });
    if (!ok) return;
    setBusy(true);
    try {
      const r = await api<SendResult>(`/notices/${noticeId}/relay`, {
        method: "POST",
        body: JSON.stringify({ group_key: groupKey, text, force }),
      });
      apply(r.relay);
      if (r.result === "sent") toast(`「${r.group_name}」に転送しました`);
      else if (r.result === "scheduled") toast(`静音時間のため ${fmtDateTime(r.send_at)} に送ります`);
      else if (r.result === "held") toast("緊急停止中のため送信を保留しました");
      else toast("送信に失敗しました。「送信の記録」をご確認ください", "error");
      onChanged();
    } catch (e) {
      const m = errorText(e);
      if (m === "already") {
        setBusy(false);
        return send(true);
      }
      toast(m, "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="grid gap-4 lg:grid-cols-2">
      {original}
      <Panel title="AI の判定と専務の確認">
        <dl className="grid grid-cols-[6.5rem_1fr] gap-y-1.5 text-sm">
          <dt className="text-ink-sub">出所</dt>
          <dd>{view.label}</dd>
          <dt className="text-ink-sub">判定</dt>
          <dd>{view.kind ? <Badge tone={view.kind === "share" ? "blue" : "gray"}>{KIND_LABELS[view.kind] ?? view.kind}</Badge> : "判定中"}</dd>
          <dt className="text-ink-sub">要約</dt>
          <dd>{view.summary || "—"}</dd>
          {view.deadline && (
            <>
              <dt className="text-ink-sub">期限</dt>
              <dd className="num">{fmtDateTime(view.deadline, false)}</dd>
            </>
          )}
          <dt className="text-ink-sub">状況</dt>
          <dd className="space-y-0.5">
            <Badge tone={state.tone}>{state.label}</Badge>
            {view.card_sent_at && (
              <div className="num text-xs text-ink-sub">
                {fmtDateTime(view.card_sent_at)} {view.kind === "officer" ? "に専務へお知らせ" : "に確認カードを送信"}
              </div>
            )}
            {view.approved_at && <div className="num text-xs text-ink-sub">{fmtDateTime(view.approved_at)} に承認</div>}
            {view.sent_at && (
              <div className="num text-xs font-bold text-line-ink">
                {fmtDateTime(view.sent_at)} に「{view.target_group_name}」へ転送
              </div>
            )}
            {view.post?.status === "pending" && (
              <div className="num text-xs text-ink-sub">{fmtDateTime(view.post.send_at)} に「{view.target_group_name}」へ送ります</div>
            )}
            {view.post?.status === "failed" && <div className="text-xs text-danger">送信に失敗しました: {view.post.error}</div>}
          </dd>
        </dl>
        <p className="mt-3 text-xs text-ink-faint">※ 判定は AI の目安です。対象外・期限切れ・送らないにしたものも、下から転送できます。</p>
      </Panel>
      </div>

      <Panel title="グループへ転送">
        {view.groups.length === 0 ? (
          <EmptyState title="転送先のグループが決まっていません">設定の「案内先のグループ」で、会員全員のグループを選んでください。</EmptyState>
        ) : (
          <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_18rem]">
            <Field
              label="転送する文面（送る前に直せます。先頭に @All を付けて送ります）"
              hint={
                view.edited || text !== view.default_text ? (
                  <button type="button" className="font-bold text-line-ink hover:underline" onClick={() => setText(view.default_text)}>
                    元の文面（見出し＋原文）に戻す
                  </button>
                ) : undefined
              }
            >
              <textarea className="h-56 w-full" value={text} onChange={(e) => setText(e.target.value)} />
            </Field>
            <div>
              <span className="field-label">送り先</span>
              <Segmented value={groupKey} onChange={setGroupKey} options={view.groups.map((g) => ({ value: g.key, label: g.label }))} />
              <div className="mt-2 rounded-xl bg-gray-50 px-3 py-2 text-sm">
                <b>{group?.group_name}</b>
                <div className="text-xs text-ink-sub">グループの全員に @All で届きます。取り消せません。</div>
              </div>
              <div className="mt-3 flex justify-end">
                <Button variant="primary" disabled={busy || !text.trim() || !group} onClick={() => send(done)}>
                  {busy ? "送信中…" : done ? "もう一度送る" : "グループへ転送する"}
                </Button>
              </div>
            </div>
          </div>
        )}
      </Panel>
    </>
  );
}
