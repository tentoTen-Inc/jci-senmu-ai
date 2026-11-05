// 通知テンプレート（docs/meeting-notice-design.md §4）。LINE・メールの送信文を登録・編集する。
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { Badge, Button, EmptyState, ErrorNote, Field, Loading, PageHeader, Panel, fmtDateTime, useUi } from "../components/ui";

type Template = {
  template_id: string;
  name: string;
  channel: "line" | "email";
  purpose: string;
  event_types: string[];
  subject: string;
  body: string;
  mention: "none" | "all";
  is_default: boolean;
  builtin: boolean;
  archived: boolean;
  updated_at: string | null;
  updated_by: string | null;
};
type ListResponse = { templates: Template[]; fields: string[]; lists: Record<string, string[]> };
type EventRow = { event_id: string; title: string; datetime_start: string; status: string };

const PURPOSES: Record<string, string> = {
  agenda: "アジェンダの配信",
  meeting_notice: "開催案内",
  correction: "修正の再案内",
};
const EVENT_TYPES = ["例会", "理事会", "五役会", "委員会", "総会", "行事", "対外"];
const FIELD_LABELS: Record<string, string> = {
  "lom.name": "LOM 名",
  "lom.short_name": "LOM の短い名前",
  "lom.bot_name": "ボット名",
  fiscal_year: "年度",
  "event.title": "会議名",
  "event.date": "開催日（11月5日(木)）",
  "event.date_wareki": "開催日（令和8年11月5日(木)）",
  "event.time": "時刻（19:00〜21:00）",
  "event.place": "場所",
  "event.online_url": "オンラインの URL",
  "agenda.url": "アジェンダの URL",
  "opinion.url": "意見書の URL",
  late: "締切を過ぎたとき（ブロック）",
  online: "オンラインがあるとき（ブロック）",
  agenda: "アジェンダがあるとき（ブロック）",
  opinion: "意見書があるとき（ブロック）",
  audience: "宛名（理事各位）",
  "sender.title": "差出人の肩書",
  "sender.name": "差出人の名前",
  doc_number: "文書番号",
  issued_on: "発信日（11月6日(金)）",
  issued_on_wareki: "発信日（令和8年11月6日）",
};
/** メールの行の頭に書く寄せ方（docs/meeting-notice-design.md §4.3） */
const MARKS: { mark: string; label: string }[] = [
  { mark: "[右] ", label: "右寄せ" },
  { mark: "[中] ", label: "中央" },
  { mark: "[題] ", label: "題（中央・大きく・太字）" },
  { mark: "[字下げ] ", label: "字下げ" },
];
type Preview = { errors: string[]; text: string | null; html?: string | null; subject?: string | null };
const BLOCKS = new Set(["late", "online", "agenda", "opinion"]);

const EMPTY: Omit<Template, "template_id" | "builtin" | "updated_at" | "updated_by"> = {
  name: "",
  channel: "line",
  purpose: "agenda",
  event_types: [],
  subject: "",
  body: "",
  mention: "all",
  is_default: false,
  archived: false,
};

export default function NoticeTemplates() {
  const { toast, confirm } = useUi();
  const [data, setData] = useState<ListResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [editing, setEditing] = useState<(typeof EMPTY & { template_id?: string; builtin?: boolean }) | null>(null);
  const [events, setEvents] = useState<EventRow[]>([]);
  const [previewEvent, setPreviewEvent] = useState("");
  const [preview, setPreview] = useState<Preview | null>(null);
  const [saving, setSaving] = useState(false);
  const bodyRef = useRef<HTMLTextAreaElement>(null);

  const load = useCallback(async () => {
    try {
      setData(await api<ListResponse>("/notice-templates?include_archived=true"));
    } catch (e) {
      setErr((e as Error).message);
    }
  }, []);
  useEffect(() => {
    load();
    api<EventRow[]>("/events?include_previous=true")
      .then((rows) => {
        const today = new Date().toISOString().slice(0, 10);
        const upcoming = rows.filter((r) => r.status !== "cancelled" && r.datetime_start.slice(0, 10) >= today);
        upcoming.sort((a, b) => a.datetime_start.localeCompare(b.datetime_start));
        setEvents(upcoming.slice(0, 30));
      })
      .catch(() => setEvents([]));
  }, [load]);

  // 書きかけの本文をプレビュー（少し待ってから）
  useEffect(() => {
    if (!editing) return;
    const t = window.setTimeout(async () => {
      try {
        setPreview(
          await api("/notice-templates/preview", {
            method: "POST",
            body: JSON.stringify({ body: editing.body, subject: editing.subject, channel: editing.channel, event_id: previewEvent || null }),
          })
        );
      } catch (e) {
        setPreview({ errors: [(e as Error).message], text: null });
      }
    }, 400);
    return () => window.clearTimeout(t);
  }, [editing, previewEvent]);

  function insert(token: string) {
    if (!editing) return;
    const el = bodyRef.current;
    const at = el ? el.selectionStart : editing.body.length;
    const body = editing.body.slice(0, at) + token + editing.body.slice(el ? el.selectionEnd : at);
    setEditing({ ...editing, body });
    window.setTimeout(() => {
      el?.focus();
      el?.setSelectionRange(at + token.length, at + token.length);
    }, 0);
  }

  async function save() {
    if (!editing) return;
    setSaving(true);
    try {
      const { template_id, builtin: _b, ...body } = editing;
      if (template_id) await api(`/notice-templates/${template_id}`, { method: "PUT", body: JSON.stringify(body) });
      else await api("/notice-templates", { method: "POST", body: JSON.stringify(body) });
      toast("テンプレートを保存しました");
      setEditing(null);
      load();
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setSaving(false);
    }
  }

  async function reset(t: Template) {
    const ok = await confirm({ title: "既定の文面に戻しますか？", body: `「${t.name}」を、最初に入っていた文面に戻します。`, confirmLabel: "戻す" });
    if (!ok) return;
    try {
      await api(`/notice-templates/${t.template_id}/reset`, { method: "POST" });
      toast("既定の文面に戻しました");
      setEditing(null);
      load();
    } catch (e) {
      toast((e as Error).message, "error");
    }
  }

  const list = data?.templates ?? [];
  return (
    <div>
      <PageHeader
        title="通知テンプレート"
        description="会議の案内などで送る LINE・メールの文面です。{{会議名}} のような差し込み項目は、送るときに会議の内容に置き換わります。"
        actions={!editing && <Button variant="primary" onClick={() => { setEditing({ ...EMPTY }); setPreview(null); }}>新しく作る</Button>}
      />
      {err && <ErrorNote>{err}</ErrorNote>}
      {!data && !err && <Loading />}

      {editing && data && (
        <Panel title={editing.template_id ? "テンプレートを直す" : "新しいテンプレート"}>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="名前">
              <input className="w-full" value={editing.name} onChange={(e) => setEditing({ ...editing, name: e.target.value })} />
            </Field>
            <Field label="使うところ">
              <select className="w-full" value={editing.purpose} onChange={(e) => setEditing({ ...editing, purpose: e.target.value })}>
                {Object.entries(PURPOSES).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
              </select>
            </Field>
            <Field label="送り方" hint="メールは LOM の Gmail に下書きを作ります">
              <select className="w-full" value={editing.channel} onChange={(e) => setEditing({ ...editing, channel: e.target.value as Template["channel"] })}>
                <option value="line">LINE</option>
                <option value="email">メール</option>
              </select>
            </Field>
            {editing.channel === "line" && (
              <Field label="メンション">
                <select className="w-full" value={editing.mention} onChange={(e) => setEditing({ ...editing, mention: e.target.value as Template["mention"] })}>
                  <option value="all">先頭で全員にメンションする（グループに送るとき）</option>
                  <option value="none">メンションしない</option>
                </select>
              </Field>
            )}
            {editing.channel === "email" && (
              <Field label="件名">
                <input className="w-full" value={editing.subject} onChange={(e) => setEditing({ ...editing, subject: e.target.value })} />
              </Field>
            )}
          </div>
          <div className="mt-3">
            <div className="mb-1 text-sm font-bold">使う会議の種類</div>
            <div className="flex flex-wrap gap-3 text-sm">
              {EVENT_TYPES.map((t) => (
                <label key={t} className="flex items-center gap-1">
                  <input
                    type="checkbox"
                    checked={editing.event_types.includes(t)}
                    onChange={(e) =>
                      setEditing({
                        ...editing,
                        event_types: e.target.checked ? [...editing.event_types, t] : editing.event_types.filter((x) => x !== t),
                      })
                    }
                  />
                  {t}
                </label>
              ))}
            </div>
            <div className="mt-1 text-xs text-ink-sub">何も選ばなければ、すべての会議で使えます</div>
          </div>

          <div className="mt-3 grid gap-4 lg:grid-cols-2">
            <div>
              <Field label="本文">
                <textarea ref={bodyRef} className="h-72 w-full font-mono text-sm" value={editing.body}
                  onChange={(e) => setEditing({ ...editing, body: e.target.value })} />
              </Field>
              <div className="mt-2 text-xs text-ink-sub">差し込み項目（押すとカーソルの位置に入ります）</div>
              <div className="mt-1 flex flex-wrap gap-1">
                {data.fields.map((f) => (
                  <Button key={f} size="sm" variant="ghost" title={`{{${f}}}`}
                    onClick={() => insert(BLOCKS.has(f) ? `{{#${f}}}\n{{/${f}}}` : `{{${f}}}`)}>
                    {FIELD_LABELS[f] ?? f}
                  </Button>
                ))}
                <Button size="sm" variant="ghost" title="{{#proposals}}…{{/proposals}}"
                  onClick={() => insert("{{#proposals}}{{no}}　{{title}}\n{{/proposals}}")}>議案の一覧（繰り返し）</Button>
              </div>
              {editing.channel === "email" && (
                <>
                  <div className="mt-2 text-xs text-ink-sub">行の寄せ方（行の頭に入れます）</div>
                  <div className="mt-1 flex flex-wrap gap-1">
                    {MARKS.map((m) => (
                      <Button key={m.mark} size="sm" variant="ghost" title={m.mark.trim()} onClick={() => insert(m.mark)}>{m.label}</Button>
                    ))}
                    <Button size="sm" variant="ghost" title="[文字](https://…)" onClick={() => insert("[リンクの文字](https://)")}>リンク</Button>
                  </div>
                </>
              )}
              <p className="mt-2 text-xs leading-relaxed text-ink-faint">
                書き方: {"{{名前}}"} 値 ／ {"{{#名前}}…{{/名前}}"} 値があるとき（議案は議案の数だけ繰り返し）／
                {" {{^名前}}…{{/名前}}"} 値がないとき ／ {"{{?名前}}"} 値がなければその行ごと消す。
                議案の中では {"{{no}} {{title}} {{url}} {{presenter}}"} が使えます。
                {editing.channel === "email" &&
                  " メールは行の頭の [右] [中] [題] [字下げ] で寄せ方を決め、[文字](https://…) でリンクにします。何もなければ左寄せです。"}
              </p>
            </div>
            <div>
              <Field label="プレビューに使う会議">
                <select className="w-full" value={previewEvent} onChange={(e) => setPreviewEvent(e.target.value)}>
                  <option value="">例の値（架空）</option>
                  {events.map((ev) => (
                    <option key={ev.event_id} value={ev.event_id}>{fmtDateTime(ev.datetime_start, false)} {ev.title}</option>
                  ))}
                </select>
              </Field>
              <div className="mt-2">
                {preview?.errors.length ? (
                  <ErrorNote>{preview.errors.join("／")}</ErrorNote>
                ) : editing.channel === "email" ? (
                  <div className="rounded-2xl border border-gray-200">
                    <div className="border-b border-gray-200 px-4 py-2 text-sm">
                      <span className="text-ink-sub">件名: </span>
                      <span className="font-bold">{preview?.subject ?? ""}</span>
                    </div>
                    {/* 送る HTML をそのまま見せる（スクリプトは動かさない） */}
                    <iframe title="メールのプレビュー" sandbox="" className="h-96 w-full rounded-b-2xl bg-white" srcDoc={preview?.html ?? ""} />
                  </div>
                ) : (
                  <div className="min-h-24 whitespace-pre-wrap break-all rounded-2xl rounded-tl-sm bg-line-soft px-4 py-3 text-sm leading-relaxed">
                    {editing.channel === "line" && editing.mention === "all" && <span className="font-bold text-line-ink">@All </span>}
                    {preview?.text ?? ""}
                  </div>
                )}
              </div>
            </div>
          </div>

          <div className="mt-4 flex flex-wrap items-center justify-between gap-2">
            <div className="flex flex-wrap gap-4 text-sm">
              <label className="flex items-center gap-1">
                <input type="checkbox" checked={editing.is_default} onChange={(e) => setEditing({ ...editing, is_default: e.target.checked })} />
                この使い方で最初に選ぶ
              </label>
              {editing.template_id && (
                <label className="flex items-center gap-1">
                  <input type="checkbox" checked={editing.archived} onChange={(e) => setEditing({ ...editing, archived: e.target.checked })} />
                  使わない（一覧で隠す）
                </label>
              )}
            </div>
            <div className="flex gap-2">
              {editing.builtin && editing.template_id && (
                <Button variant="ghost" onClick={() => reset(list.find((t) => t.template_id === editing.template_id)!)}>既定の文面に戻す</Button>
              )}
              <Button onClick={() => setEditing(null)}>やめる</Button>
              <Button variant="primary" onClick={save} disabled={saving}>{saving ? "保存中…" : "保存する"}</Button>
            </div>
          </div>
        </Panel>
      )}

      {data && !editing && (
        <Panel flush>
          {list.length === 0 ? (
            <div className="px-5 py-3"><EmptyState title="テンプレートがありません" /></div>
          ) : (
            <div className="overflow-x-auto">
              <table className="tbl min-w-[620px]">
                <thead>
                  <tr><th>名前</th><th>使うところ</th><th>送り方</th><th>会議の種類</th><th>更新</th><th /></tr>
                </thead>
                <tbody>
                  {list.map((t) => (
                    <tr key={t.template_id} className={t.archived ? "opacity-50" : ""}>
                      <td className="font-bold">
                        {t.name}
                        {t.is_default && <span className="ml-2"><Badge tone="green">最初に選ぶ</Badge></span>}
                        {t.archived && <span className="ml-2"><Badge>使わない</Badge></span>}
                      </td>
                      <td>{PURPOSES[t.purpose] ?? t.purpose}</td>
                      <td>{t.channel === "line" ? "LINE" : "メール"}</td>
                      <td className="text-ink-sub">{t.event_types.length ? t.event_types.join("・") : "すべて"}</td>
                      <td className="num text-xs text-ink-sub">{t.updated_at ? fmtDateTime(t.updated_at) : t.builtin ? "既定" : "—"}</td>
                      <td className="text-right">
                        <Button size="sm" onClick={() => { setEditing({ ...t }); setPreview(null); }}>直す</Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>
      )}
    </div>
  );
}
