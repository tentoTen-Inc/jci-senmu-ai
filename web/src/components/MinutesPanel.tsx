// 会議の「議事録」（docs/meeting-minutes-design.md §7）。録画から AI で案を作り、人が確かめて直してから送る
import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client";
import { Badge, Button, ErrorNote, Field, Loading, Panel, Progress, fmtDateTime, useUi } from "./ui";

type Opinion = { by: string | null; target: string | null; point: string; response: string | null; response_by: string | null };
type ActionItem = { owner: string | null; what: string; due: string | null };
type Section = {
  proposal: string | null;
  title: string;
  explained: string[];
  opinions: Opinion[];
  decided: string[];
  action_items: ActionItem[];
  for_absent: string | null;
};
type Segment = { start: string; speaker: string | null; speaker_by: string | null; text: string; screen: string | null; evidence: string | null };
type Unclear = { unclear_id: string; at: string; kind: string; question: string; context: string; status: string; answer: string | null; evidence: string | null };
type Sensitive = { sensitive_id: string; at: string | null; kind: string; summary: string; include: boolean };
type Send = { kind: string; channel: string; to_name: string | null; committee: string | null; text: string; sent_at: string | null; sent_by: string | null };
type Minutes = {
  status: "queued" | "copying" | "transcribing" | "verifying" | "drafting" | "ready" | "failed";
  recording_name: string | null;
  segments: Segment[];
  unclear: Unclear[];
  sections: Section[];
  confirmations: string[];
  sensitive: Sensitive[];
  committees: { committee: string; text: string; created_at: string | null }[];
  sends: Send[];
  error: string | null;
  updated_at: string | null;
};
type CommitteeDest = {
  committee: string;
  chair: { member_id: string; name: string; linked: boolean } | null;
  group: { group_id: string; group_name: string } | null;
};
type View = {
  configured: boolean;
  attendees: { member_id: string; name: string; absent: boolean; nicknames: string[] }[];
  minutes: Minutes | null;
  names?: Record<string, string>;
  progress?: { chunk: number; chunks: number | null };
  recording_url?: string | null;
  doc_url?: string | null;
  group_preview?: string | null;
  group?: { group_id: string | null; group_name: string | null } | null;
  committees?: CommitteeDest[];
};

const STEPS: Record<Minutes["status"], string> = {
  queued: "受付",
  copying: "録画を写しています",
  transcribing: "書き起こしています",
  verifying: "画面で確かめています",
  drafting: "まとめています",
  ready: "案あり",
  failed: "止まりました",
};
const SENSITIVE_KINDS: Record<string, string> = { personnel: "人事", candidate: "入会候補", private: "個人の事情", money: "お金" };
const UNCLEAR_KINDS: Record<string, string> = { speaker: "話者", screen: "画面", word: "語" };

/** 録画の時刻（HH:MM:SS）の位置で開く */
function atUrl(url: string | null | undefined, at: string): string | undefined {
  if (!url) return undefined;
  const [h, m, s] = at.split(":").map((x) => parseInt(x, 10) || 0);
  return `${url}?t=${h * 3600 + m * 60 + s}`;
}

const lines = (text: string) => text.split("\n").map((x) => x.trim()).filter(Boolean);

export default function MinutesPanel({ eventId }: { eventId: string }) {
  const { confirm, toast } = useUi();
  const [view, setView] = useState<View | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [recording, setRecording] = useState("");
  const [draft, setDraft] = useState<{ sections: Section[]; confirmations: string; include: string[] } | null>(null);

  const load = useCallback(async () => {
    try {
      const v = await api<View>(`/events/${eventId}/minutes`);
      setView(v);
      setErr(null);
      const m = v.minutes;
      if (m?.status === "ready") {
        setDraft({
          sections: m.sections,
          confirmations: m.confirmations.join("\n"),
          include: m.sensitive.filter((x) => x.include).map((x) => x.sensitive_id),
        });
      }
    } catch (e) {
      setErr((e as Error).message);
    }
  }, [eventId]);
  useEffect(() => {
    load();
  }, [load]);

  // 作っている間は 15 秒ごとに読み直す
  const working = !!view?.minutes && !["ready", "failed"].includes(view.minutes.status);
  useEffect(() => {
    if (!working) return;
    const id = window.setInterval(load, 15000);
    return () => window.clearInterval(id);
  }, [working, load]);

  async function call(path: string, body?: unknown, done?: string) {
    setBusy(true);
    try {
      const v = await api<View>(`/events/${eventId}/minutes${path}`, { method: "POST", body: body ? JSON.stringify(body) : undefined });
      setView(v);
      if (v.minutes?.status === "ready") {
        setDraft({
          sections: v.minutes.sections,
          confirmations: v.minutes.confirmations.join("\n"),
          include: v.minutes.sensitive.filter((x) => x.include).map((x) => x.sensitive_id),
        });
      }
      if (done) toast(done);
      return true;
    } catch (e) {
      toast((e as Error).message, "error");
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function start() {
    const again = !!view?.minutes;
    if (again) {
      const ok = await confirm({
        title: "議事録を作り直しますか？",
        body: <p>今の案（直したところを含む）は新しい案に置き換わります。送った記録とドキュメントのリンクは残ります。</p>,
        confirmLabel: "作り直す",
        danger: true,
      });
      if (!ok) return;
    }
    if (await call("", { recording }, "議事録を作り始めました。数分〜10分ほどかかります。")) setRecording("");
  }

  async function save() {
    if (!draft) return;
    setBusy(true);
    try {
      const v = await api<View>(`/events/${eventId}/minutes`, {
        method: "PUT",
        body: JSON.stringify({ sections: draft.sections, confirmations: lines(draft.confirmations), include_sensitive: draft.include }),
      });
      setView(v);
      toast("議事録の案を保存しました");
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(false);
    }
  }

  async function sendGroup() {
    const g = view?.group;
    if (!g?.group_id) return;
    const sent = view?.minutes?.sends.filter((s) => s.kind === "group") ?? [];
    const ok = await confirm({
      title: "グループに議事録を送りますか？",
      body: (
        <>
          <p>
            送り先: <b>LINE グループ「{g.group_name}」</b>（全員と、欠席者・宿題の担当者にメンション）
          </p>
          {sent.length > 0 && <p className="mt-1 text-danger">{fmtDateTime(sent[sent.length - 1].sent_at)} に送っています。もう一度送ります。</p>}
          <p className="mt-1">グループへの投稿は取り消せません。</p>
        </>
      ),
      confirmLabel: "送る",
    });
    if (ok) await call("/send-group", { expected_group_id: g.group_id }, `「${g.group_name}」に送りました`);
  }

  if (err) return <Panel title="議事録"><ErrorNote>{err}</ErrorNote></Panel>;
  if (!view) return <Panel title="議事録"><Loading /></Panel>;
  const m = view.minutes;
  const names = view.names ?? {};
  const dirty =
    !!m && !!draft && m.status === "ready" &&
    (JSON.stringify(draft.sections) !== JSON.stringify(m.sections) ||
      draft.confirmations !== m.confirmations.join("\n") ||
      JSON.stringify([...draft.include].sort()) !== JSON.stringify(m.sensitive.filter((x) => x.include).map((x) => x.sensitive_id).sort()));

  return (
    <Panel
      title="議事録"
      action={m && <Badge tone={m.status === "ready" ? "green" : m.status === "failed" ? "red" : "gray"}>{STEPS[m.status]}</Badge>}
    >
      {!view.configured && (
        <p className="mb-3 text-sm text-hold">議事録を作る設定がまだありません（scripts/setup_minutes.sh）。</p>
      )}
      <p className="text-sm text-ink-sub">
        Meet の録画から AI で議事録の案を作ります。音声でわからない所は、その時刻の画面（話している人の枠・共有画面の資料）も見て確かめます。
        送るのは、確かめて直した後です。
      </p>
      <div className="mt-3 flex flex-wrap items-end gap-2">
        <Field label="録画の Google ドライブの URL（ファイルかフォルダ）" className="min-w-[260px] flex-1">
          <input className="w-full" placeholder="https://drive.google.com/…" value={recording} onChange={(e) => setRecording(e.target.value)} />
        </Field>
        <Button variant={m ? "secondary" : "primary"} onClick={start} disabled={busy || !recording.trim() || !view.configured || working}>
          {m ? "作り直す" : "議事録を作る"}
        </Button>
      </div>

      {m && (
        <div className="mt-3 rounded-2xl bg-gray-50 px-4 py-3 text-sm">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-ink-sub">録画:</span>
            {view.recording_url ? (
              <a className="text-line-ink underline" href={view.recording_url} target="_blank" rel="noreferrer">{m.recording_name ?? "録画"}</a>
            ) : (
              <span>{m.recording_name}</span>
            )}
            {view.doc_url && (
              <a className="ml-auto text-line-ink underline" href={view.doc_url} target="_blank" rel="noreferrer">議事録のドキュメント</a>
            )}
          </div>
          {working && (
            <div className="mt-2">
              <div className="text-xs text-ink-sub">
                {STEPS[m.status]}
                {m.status === "transcribing" && view.progress?.chunks ? `（${view.progress.chunk} / ${view.progress.chunks}）` : ""}…
              </div>
              {m.status === "transcribing" && view.progress?.chunks ? <Progress value={view.progress.chunk} max={view.progress.chunks} /> : null}
            </div>
          )}
          {m.error && (
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <span className="text-xs text-danger">{m.error}</span>
              <Button size="sm" onClick={() => call("/resume", undefined, "続きから始めました")} disabled={busy}>続きから</Button>
            </div>
          )}
        </div>
      )}

      {m?.status === "ready" && draft && (
        <>
          <UnclearList view={view} busy={busy} onResolve={(id, body) => call(`/unclear/${id}`, body, "直しました")} />

          <div className="mt-6 border-t border-gray-100 pt-4">
            <div className="mb-2 text-sm font-bold">議事録の案</div>
            <div className="space-y-4">
              {draft.sections.map((s, i) => (
                <SectionEditor
                  key={i}
                  section={s}
                  attendees={view.attendees}
                  onChange={(next) => setDraft({ ...draft, sections: draft.sections.map((x, j) => (j === i ? next : x)) })}
                  onRemove={() => setDraft({ ...draft, sections: draft.sections.filter((_, j) => j !== i) })}
                />
              ))}
            </div>
            <Field label="確認事項（1行に1つ）" className="mt-4">
              <textarea className="w-full" rows={3} value={draft.confirmations} onChange={(e) => setDraft({ ...draft, confirmations: e.target.value })} />
            </Field>

            {m.sensitive.length > 0 && (
              <div className="mt-4 rounded-2xl bg-hold/10 px-4 py-3">
                <div className="text-sm font-bold">内々の話（グループ向けに入れるものだけ選ぶ）</div>
                <ul className="mt-2 space-y-1 text-sm">
                  {m.sensitive.map((x) => (
                    <li key={x.sensitive_id}>
                      <label className="flex items-start gap-2">
                        <input
                          type="checkbox"
                          checked={draft.include.includes(x.sensitive_id)}
                          onChange={(e) =>
                            setDraft({
                              ...draft,
                              include: e.target.checked ? [...draft.include, x.sensitive_id] : draft.include.filter((id) => id !== x.sensitive_id),
                            })
                          }
                        />
                        <span>
                          <Badge tone="gray">{SENSITIVE_KINDS[x.kind] ?? x.kind}</Badge> {x.summary}
                          {x.at && (
                            <a className="ml-1 text-xs text-line-ink underline" href={atUrl(view.recording_url, x.at)} target="_blank" rel="noreferrer">{x.at}</a>
                          )}
                        </span>
                      </label>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            <div className="mt-4 flex flex-wrap justify-end gap-2">
              <Button onClick={() => load()} disabled={busy || !dirty}>直したところを戻す</Button>
              <Button variant="primary" onClick={save} disabled={busy || !dirty}>案を保存</Button>
            </div>
          </div>

          <div className="mt-6 border-t border-gray-100 pt-4">
            <div className="mb-2 flex flex-wrap items-center gap-2">
              <span className="text-sm font-bold">グループ向けの要点</span>
              {view.group?.group_name && <span className="text-xs text-ink-sub">送り先: LINE グループ「{view.group.group_name}」</span>}
            </div>
            <div className="max-h-96 overflow-y-auto whitespace-pre-wrap break-all rounded-2xl rounded-tl-sm bg-line-soft px-4 py-3 text-sm leading-relaxed">
              {view.group_preview}
            </div>
            {!view.group?.group_id && <p className="mt-2 text-xs text-hold">この会議体の LINE グループが設定にありません（設定 → LINE グループ）。</p>}
            <div className="mt-3 flex flex-wrap justify-end gap-2">
              <Button onClick={() => call("/doc", undefined, "ドキュメントに保存しました")} disabled={busy || dirty}>
                {view.doc_url ? "ドキュメントを更新" : "ドキュメントに保存"}
              </Button>
              <Button variant="primary" onClick={sendGroup} disabled={busy || dirty || !view.group?.group_id}>グループに送る</Button>
            </div>
            {dirty && <p className="mt-1 text-right text-xs text-ink-sub">案を保存してから保存・送信してください。</p>}
          </div>

          <CommitteeSection view={view} busy={busy || dirty} call={call} />

          <details className="mt-6 text-sm">
            <summary className="cursor-pointer font-bold text-ink-sub">書き起こし（{m.segments.length}件）</summary>
            <ul className="mt-2 max-h-[480px] space-y-1 overflow-y-auto">
              {m.segments.map((s, i) => (
                <li key={i} className="text-xs leading-relaxed">
                  <a className="num text-line-ink underline" href={atUrl(view.recording_url, s.start)} target="_blank" rel="noreferrer">{s.start}</a>{" "}
                  <b>{s.speaker ? names[s.speaker] : "（話者不明）"}</b>：{s.text}
                  {s.evidence && <span className="ml-1 text-ink-faint">［{s.evidence}］</span>}
                </li>
              ))}
            </ul>
          </details>
        </>
      )}

      {m && m.sends.length > 0 && (
        <details className="mt-4 text-sm">
          <summary className="cursor-pointer font-bold text-ink-sub">送った記録（{m.sends.length}件）</summary>
          <ul className="mt-2 space-y-2">
            {[...m.sends].reverse().map((s, i) => (
              <li key={i} className="rounded-xl bg-gray-50 px-3 py-2">
                <div className="num text-xs text-ink-sub">
                  {fmtDateTime(s.sent_at)}・{s.committee ? `${s.committee}向け・` : ""}「{s.to_name}」・{s.sent_by}
                </div>
                <div className="mt-1 max-h-40 overflow-y-auto whitespace-pre-wrap break-all text-xs">{s.text}</div>
              </li>
            ))}
          </ul>
        </details>
      )}
    </Panel>
  );
}

/** 確かめきれなかった所。人が録画を見て直す（話者を選ぶと呼び名として覚えさせることもできる） */
function UnclearList({
  view,
  busy,
  onResolve,
}: {
  view: View;
  busy: boolean;
  onResolve: (id: string, body: { status: string; speaker?: string | null; text?: string | null; remember_nickname?: string | null }) => void;
}) {
  const m = view.minutes!;
  const open = m.unclear.filter((u) => u.status === "open" || u.status === "skipped");
  const verified = m.unclear.filter((u) => u.status === "verified");
  return (
    <div className="mt-6 border-t border-gray-100 pt-4">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <span className="text-sm font-bold">不明点</span>
        <Badge tone={open.length ? "amber" : "green"}>残り {open.length}件</Badge>
        {verified.length > 0 && <span className="text-xs text-ink-sub">画面で確かめて直したもの {verified.length}件</span>}
      </div>
      {open.length === 0 && <p className="text-sm text-ink-sub">残っている不明点はありません。</p>}
      <ul className="space-y-3">
        {open.map((u) => (
          <UnclearItem key={u.unclear_id} item={u} view={view} busy={busy} onResolve={onResolve} />
        ))}
      </ul>
      {verified.length > 0 && (
        <details className="mt-3 text-xs">
          <summary className="cursor-pointer text-ink-sub">画面で確かめた所（根拠）</summary>
          <ul className="mt-2 space-y-1">
            {verified.map((u) => (
              <li key={u.unclear_id}>
                <a className="num text-line-ink underline" href={atUrl(view.recording_url, u.at)} target="_blank" rel="noreferrer">{u.at}</a>{" "}
                {u.question} → <b>{u.answer}</b>
                <span className="ml-1 text-ink-faint">［{u.evidence}］</span>
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

function UnclearItem({
  item,
  view,
  busy,
  onResolve,
}: {
  item: Unclear;
  view: View;
  busy: boolean;
  onResolve: (id: string, body: { status: string; speaker?: string | null; text?: string | null; remember_nickname?: string | null }) => void;
}) {
  const [speaker, setSpeaker] = useState("");
  const [text, setText] = useState("");
  const [nickname, setNickname] = useState("");
  return (
    <li className="rounded-2xl bg-gray-50 px-4 py-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <a className="num font-bold text-line-ink underline" href={atUrl(view.recording_url, item.at)} target="_blank" rel="noreferrer">{item.at}</a>
        <Badge tone="gray">{UNCLEAR_KINDS[item.kind] ?? item.kind}</Badge>
        <span className="font-bold">{item.question}</span>
      </div>
      {item.context && <div className="mt-1 text-xs text-ink-sub">前後: {item.context}</div>}
      {item.answer && <div className="mt-1 text-xs text-ink-sub">AI: {item.answer}</div>}
      <div className="mt-2 grid gap-2 sm:grid-cols-3">
        <select value={speaker} onChange={(e) => setSpeaker(e.target.value)}>
          <option value="">話者を選ぶ（直すとき）</option>
          {view.attendees.map((a) => (
            <option key={a.member_id} value={a.member_id}>{a.name}</option>
          ))}
        </select>
        <input placeholder="発言を直す（任意）" value={text} onChange={(e) => setText(e.target.value)} />
        <input placeholder="呼ばれ方を覚える（例: 〇〇くん）" value={nickname} onChange={(e) => setNickname(e.target.value)} disabled={!speaker} />
      </div>
      <div className="mt-2 flex justify-end gap-2">
        <Button size="sm" onClick={() => onResolve(item.unclear_id, { status: "skipped" })} disabled={busy || item.status === "skipped"}>このままにする</Button>
        <Button
          size="sm"
          variant="primary"
          onClick={() => onResolve(item.unclear_id, { status: "resolved", speaker: speaker || null, text: text || null, remember_nickname: nickname || null })}
          disabled={busy || (!speaker && !text.trim())}
        >
          直す
        </Button>
      </div>
    </li>
  );
}

/** 議案ごとのまとめ。意見・宿題の人は出席予定者から選ぶ */
function SectionEditor({
  section,
  attendees,
  onChange,
  onRemove,
}: {
  section: Section;
  attendees: View["attendees"];
  onChange: (s: Section) => void;
  onRemove: () => void;
}) {
  const person = (value: string | null, set: (v: string | null) => void, empty: string) => (
    <select value={value ?? ""} onChange={(e) => set(e.target.value || null)}>
      <option value="">{empty}</option>
      {attendees.map((a) => (
        <option key={a.member_id} value={a.member_id}>{a.name}</option>
      ))}
    </select>
  );
  return (
    <div className="rounded-2xl border border-gray-200 px-4 py-3">
      <div className="flex items-center gap-2">
        <input className="flex-1 font-bold" value={section.title} onChange={(e) => onChange({ ...section, title: e.target.value })} />
        <Button size="sm" onClick={onRemove}>消す</Button>
      </div>
      <div className="mt-2 grid gap-2 sm:grid-cols-2">
        <Field label="説明の要点（1行に1つ）">
          <textarea className="w-full" rows={3} value={section.explained.join("\n")} onChange={(e) => onChange({ ...section, explained: lines(e.target.value) })} />
        </Field>
        <Field label="決まったこと（1行に1つ）">
          <textarea className="w-full" rows={3} value={section.decided.join("\n")} onChange={(e) => onChange({ ...section, decided: lines(e.target.value) })} />
        </Field>
      </div>
      <div className="mt-2 text-xs font-bold text-ink-sub">意見</div>
      <ul className="mt-1 space-y-2">
        {section.opinions.map((o, i) => {
          const set = (patch: Partial<Opinion>) => onChange({ ...section, opinions: section.opinions.map((x, j) => (j === i ? { ...x, ...patch } : x)) });
          return (
            <li key={i} className="grid gap-2 sm:grid-cols-[160px_1fr_1fr_auto]">
              {person(o.by, (v) => set({ by: v }), "（話者不明）")}
              <input placeholder="意見" value={o.point} onChange={(e) => set({ point: e.target.value })} />
              <input placeholder="答え（任意）" value={o.response ?? ""} onChange={(e) => set({ response: e.target.value || null })} />
              <Button size="sm" onClick={() => onChange({ ...section, opinions: section.opinions.filter((_, j) => j !== i) })}>消す</Button>
            </li>
          );
        })}
      </ul>
      <Button size="sm" className="mt-1" onClick={() => onChange({ ...section, opinions: [...section.opinions, { by: null, target: null, point: "", response: null, response_by: null }] })}>
        意見を足す
      </Button>
      <div className="mt-3 text-xs font-bold text-ink-sub">宿題</div>
      <ul className="mt-1 space-y-2">
        {section.action_items.map((a, i) => {
          const set = (patch: Partial<ActionItem>) =>
            onChange({ ...section, action_items: section.action_items.map((x, j) => (j === i ? { ...x, ...patch } : x)) });
          return (
            <li key={i} className="grid gap-2 sm:grid-cols-[160px_1fr_160px_auto]">
              {person(a.owner, (v) => set({ owner: v }), "担当未定")}
              <input placeholder="内容" value={a.what} onChange={(e) => set({ what: e.target.value })} />
              <input placeholder="期限（例: 次の理事会まで）" value={a.due ?? ""} onChange={(e) => set({ due: e.target.value || null })} />
              <Button size="sm" onClick={() => onChange({ ...section, action_items: section.action_items.filter((_, j) => j !== i) })}>消す</Button>
            </li>
          );
        })}
      </ul>
      <Button size="sm" className="mt-1" onClick={() => onChange({ ...section, action_items: [...section.action_items, { owner: null, what: "", due: null }] })}>
        宿題を足す
      </Button>
    </div>
  );
}

/** 委員会向け: その委員会に関わる所だけを抜き出し、委員長への個別の LINE か委員会のグループに送る */
function CommitteeSection({
  view,
  busy,
  call,
}: {
  view: View;
  busy: boolean;
  call: (path: string, body?: unknown, done?: string) => Promise<boolean>;
}) {
  const { confirm, toast } = useUi();
  const options = view.committees ?? [];
  const [committee, setCommittee] = useState(options[0]?.committee ?? "");
  const made = view.minutes?.committees.find((c) => c.committee === committee);
  const [text, setText] = useState(made?.text ?? "");
  useEffect(() => setText(made?.text ?? ""), [made?.text]);
  const dest = options.find((c) => c.committee === committee);

  async function send(channel: "user" | "group") {
    if (!dest) return;
    const to = channel === "user" ? dest.chair : dest.group;
    if (!to) return;
    const name = channel === "user" ? `${dest.chair!.name}（${committee}委員長）への個別の LINE` : `LINE グループ「${dest.group!.group_name}」`;
    const ok = await confirm({
      title: "委員会向けの議事録を送りますか？",
      body: (
        <>
          <p>
            送り先: <b>{name}</b>
          </p>
          <p className="mt-1">LINE で送ったメッセージは取り消せません。</p>
        </>
      ),
      confirmLabel: "送る",
    });
    if (!ok) return;
    const expected = channel === "user" ? dest.chair!.member_id : dest.group!.group_id;
    await call("/committee/send", { committee, channel, expected_to_id: expected, text }, "送りました");
  }

  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      toast("文面をコピーしました");
    } catch {
      toast("コピーできませんでした。文面を選んでコピーしてください。", "error");
    }
  }

  if (options.length === 0) return null;
  return (
    <div className="mt-6 border-t border-gray-100 pt-4">
      <div className="mb-2 text-sm font-bold">委員会向け</div>
      <p className="text-xs text-ink-sub">その委員会の議案だけでなく、所信・予算などの協議の中でその委員会に向いた意見も拾います。</p>
      <div className="mt-2 flex flex-wrap items-end gap-2">
        <Field label="委員会">
          <select value={committee} onChange={(e) => setCommittee(e.target.value)}>
            {options.map((c) => (
              <option key={c.committee} value={c.committee}>{c.committee}</option>
            ))}
          </select>
        </Field>
        <Button onClick={() => call("/committee", { committee }, "委員会向けの議事録を作りました")} disabled={busy || !committee}>
          {made ? "作り直す" : "委員会向けを作る"}
        </Button>
      </div>
      {made && (
        <>
          <textarea className="mt-3 w-full text-sm" rows={14} value={text} onChange={(e) => setText(e.target.value)} />
          <div className="mt-1 text-right text-xs text-ink-faint">{text.length} / 5000字</div>
          <div className="mt-2 flex flex-wrap justify-end gap-2">
            <Button onClick={copy}>文面をコピー</Button>
            <Button onClick={() => send("group")} disabled={busy || !dest?.group || !text.trim()}>
              {dest?.group ? `グループ「${dest.group.group_name}」に送る` : "委員会のグループなし"}
            </Button>
            <Button variant="primary" onClick={() => send("user")} disabled={busy || !dest?.chair?.linked || !text.trim()}>
              {dest?.chair ? (dest.chair.linked ? `${dest.chair.name}委員長に送る` : `${dest.chair.name}委員長は LINE 未連携`) : "委員長が未登録"}
            </Button>
          </div>
        </>
      )}
    </div>
  );
}
