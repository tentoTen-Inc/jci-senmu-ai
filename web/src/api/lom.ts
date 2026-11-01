// LOM ごとに変わる名前など（/api/lom。サーバーの app/lom.py と設定の line_groups）。
// 画面に LOM 名・ボット名を直書きしないために使う。一度読んだら画面を開いている間は使い回す。
import { useEffect, useState } from "react";
import { api } from "./client";

export type Lom = {
  lom_name: string;
  lom_short_name: string;
  bot_name: string;
  friend_url: string | null;
  drive_reader_sa: string;
  /** 既定の案内先の LINE グループ（goyaku / riji / all → グループ ID。以前の設定は名前） */
  line_groups: Record<string, string>;
  /** ボットが入っているグループ */
  groups: { group_id: string; group_name: string | null }[];
};

const FALLBACK: Lom = {
  lom_name: "青年会議所",
  lom_short_name: "青年会議所",
  bot_name: "専務AI",
  friend_url: null,
  drive_reader_sa: "",
  line_groups: {},
  groups: [],
};

let cache: Promise<Lom> | null = null;

export function loadLom(): Promise<Lom> {
  cache ??= api<Lom>("/lom").catch(() => {
    cache = null; // 次に開いたときに読み直す
    return FALLBACK;
  });
  return cache;
}

export function useLom(): Lom {
  const [lom, setLom] = useState<Lom>(FALLBACK);
  useEffect(() => {
    let alive = true;
    loadLom().then((l) => alive && setLom(l));
    return () => {
      alive = false;
    };
  }, []);
  return lom;
}
