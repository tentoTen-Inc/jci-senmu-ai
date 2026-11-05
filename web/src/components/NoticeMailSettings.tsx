// 設定 → 案内メール（docs/meeting-notice-design.md §6: 宛名・差出人・宛先の入れ方・文書番号・文字）
import { useEffect, useState } from "react";
import { Button, Field, Panel } from "./ui";

export type NoticeSetting = { audience: string; sender_role: string; sender_title: string; to_mode: "bcc" | "to"; cc_roles: string[] };
export type DocNumber = { enabled: boolean; format: string; prefix: string; term: string; digits: number; start_seq: Record<string, number> };
export type EmailStyle = { font_family: string; font_size: number };
export type NoticeMailValue = { meeting_notices: Record<string, NoticeSetting>; doc_number: DocNumber; email_style: EmailStyle };

const MEETING_TYPES = ["五役会", "理事会", "委員会", "例会", "総会"];
const BLANK: NoticeSetting = { audience: "", sender_role: "", sender_title: "", to_mode: "bcc", cc_roles: [] };

function formatNumber(d: DocNumber, seq: number): string {
  return d.format.replace("{prefix}", d.prefix).replace("{term}", d.term).replace("{seq}", String(seq).padStart(Math.max(d.digits, 1), "0"));
}

export default function NoticeMailSettings({
  value,
  saving,
  onSave,
}: {
  value: NoticeMailValue;
  saving: boolean;
  onSave: (patch: NoticeMailValue) => void;
}) {
  const [draft, setDraft] = useState(value);
  // CC の役職は書きかけ（「、」の後など）を残すため文字のまま持ち、保存するときに分ける
  const ccOf = (v: NoticeMailValue) => Object.fromEntries(MEETING_TYPES.map((t) => [t, (v.meeting_notices[t]?.cc_roles ?? []).join("、")]));
  const [ccText, setCcText] = useState<Record<string, string>>(() => ccOf(value));
  const years = Object.keys(value.doc_number.start_seq).sort();
  const [year, setYear] = useState(years[years.length - 1] ?? String(new Date().getFullYear() + 1));
  useEffect(() => {
    setDraft(value);
    setCcText(ccOf(value));
  }, [value]);

  const parseCc = (text: string) => text.split(/[、,，]/).map((x) => x.trim()).filter(Boolean);
  const withCc = (): NoticeMailValue => {
    const notices = { ...draft.meeting_notices };
    for (const t of MEETING_TYPES) {
      const roles = parseCc(ccText[t] ?? "");
      if (notices[t] || roles.length) notices[t] = { ...(notices[t] ?? BLANK), cc_roles: roles };
    }
    return { ...draft, meeting_notices: notices };
  };
  const dirty = JSON.stringify(withCc()) !== JSON.stringify(value);
  const notice = (t: string) => draft.meeting_notices[t] ?? BLANK;
  const setNotice = (t: string, patch: Partial<NoticeSetting>) =>
    setDraft({ ...draft, meeting_notices: { ...draft.meeting_notices, [t]: { ...notice(t), ...patch } } });
  const doc = draft.doc_number;
  const setDoc = (patch: Partial<DocNumber>) => setDraft({ ...draft, doc_number: { ...doc, ...patch } });
  const start = doc.start_seq[year] ?? 1;

  return (
    <Panel title="案内メール">
      <p className="mb-3 text-xs leading-relaxed text-ink-sub">
        会議の「案内・配信」で作るメールの宛名・差出人と、宛先の入れ方です。差出人の名前は、その役職の会員から引きます。
        宛先は既定で BCC（To は LOM のアドレス）。CC の役職は「専務理事」のように書き、複数なら「、」で区切ります。
      </p>
      <div className="overflow-x-auto">
        <table className="tbl min-w-[760px]">
          <thead>
            <tr><th>会議</th><th>宛名</th><th>差出人の役職</th><th>文中の肩書</th><th>宛先</th><th>CC の役職</th></tr>
          </thead>
          <tbody>
            {MEETING_TYPES.map((t) => {
              const n = notice(t);
              return (
                <tr key={t}>
                  <td className="whitespace-nowrap font-bold">{t}</td>
                  <td><input className="w-full" placeholder="理事各位" value={n.audience} onChange={(e) => setNotice(t, { audience: e.target.value })} /></td>
                  <td><input className="w-full" placeholder="理事長" value={n.sender_role} onChange={(e) => setNotice(t, { sender_role: e.target.value })} /></td>
                  <td><input className="w-full" placeholder="空なら役職のまま" value={n.sender_title} onChange={(e) => setNotice(t, { sender_title: e.target.value })} /></td>
                  <td>
                    <select value={n.to_mode} onChange={(e) => setNotice(t, { to_mode: e.target.value as NoticeSetting["to_mode"] })}>
                      <option value="bcc">BCC</option>
                      <option value="to">To に並べる</option>
                    </select>
                  </td>
                  <td>
                    <input className="w-full" placeholder="なし" value={ccText[t] ?? ""}
                      onChange={(e) => setCcText({ ...ccText, [t]: e.target.value })} />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div className="mt-5 text-sm font-bold">文書番号</div>
      <label className="mt-1 flex items-center gap-2 text-sm">
        <input type="checkbox" checked={doc.enabled} onChange={(e) => setDoc({ enabled: e.target.checked })} />
        案内文に文書番号を付ける（{"{{doc_number}}"}）
      </label>
      {doc.enabled && (
        <>
          <div className="mt-2 grid gap-3 sm:grid-cols-3">
            <Field label="頭の文字" hint="{prefix}">
              <input className="w-full" placeholder="○○発" value={doc.prefix} onChange={(e) => setDoc({ prefix: e.target.value })} />
            </Field>
            <Field label="期・年度の番号" hint="{term}">
              <input className="w-full" placeholder="227" value={doc.term} onChange={(e) => setDoc({ term: e.target.value })} />
            </Field>
            <Field label="書式" hint="{prefix} {term} {seq}（連番）を使えます">
              <input className="w-full" value={doc.format} onChange={(e) => setDoc({ format: e.target.value })} />
            </Field>
            <Field label="連番の桁">
              <input className="w-24" type="number" min={1} max={6} value={doc.digits} onChange={(e) => setDoc({ digits: Number(e.target.value) || 1 })} />
            </Field>
            <Field label="年度" hint="会議の年度">
              <input className="w-28" type="number" value={year} onChange={(e) => setYear(e.target.value)} />
            </Field>
            <Field label="この年度の最初の番号" hint="手で付けた番号の続きから始めるとき">
              <input className="w-24" type="number" min={1} value={start}
                onChange={(e) => setDoc({ start_seq: { ...doc.start_seq, [year]: Math.max(1, Number(e.target.value) || 1) } })} />
            </Field>
          </div>
          <p className="mt-2 text-xs text-ink-sub">
            例: <span className="num font-bold">{formatNumber(doc, start)}</span>。番号は下書きを作るときに決まり、同じ会議で作り直しても同じ番号を使います。
          </p>
        </>
      )}

      <div className="mt-5 text-sm font-bold">文字</div>
      <div className="mt-1 flex flex-wrap gap-3">
        <Field label="フォント">
          <input value={draft.email_style.font_family} onChange={(e) => setDraft({ ...draft, email_style: { ...draft.email_style, font_family: e.target.value } })} />
        </Field>
        <Field label="大きさ（pt）">
          <input className="w-24" type="number" min={8} max={20} value={draft.email_style.font_size}
            onChange={(e) => setDraft({ ...draft, email_style: { ...draft.email_style, font_size: Number(e.target.value) || 11 } })} />
        </Field>
      </div>

      <div className="mt-3 flex justify-end">
        <Button disabled={saving || !dirty} onClick={() => onSave(withCc())}>保存</Button>
      </div>
    </Panel>
  );
}
