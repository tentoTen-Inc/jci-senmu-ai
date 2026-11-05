---
name: 開催案内（メール）
channel: email
purpose: meeting_notice
is_default: true
subject: 【{{fiscal_year}}年度】{{event.title}}開催のご案内
---
[右] {{issued_on_wareki}}
{{lom.name}}
{{?audience}}
[右] {{?doc_number}}
[右] {{lom.name}}
[右] {{sender.title}}　{{?sender.name}}

[題] {{fiscal_year}}年度 {{event.title}}開催のご案内

[字下げ] 皆様におかれましては、ますますご健勝のことと存じます。
[字下げ] 下記の通り{{event.title}}を開催いたします。
[字下げ] ご多忙のところ恐縮ですが、ご出席くださいますようお願い申し上げます。

[中] 記

日　　時　　　{{event.date_wareki}}　{{event.time}}

場　　所　　　{{event.place}}{{#online}}（[オンラインで参加]({{event.online_url}})）{{/online}}

{{#proposals}}{{#first}}内　　容　　　{{/first}}{{^first}}　　　　　　　{{/first}}{{no}}　{{title}}
{{/proposals}}
{{#agenda}}資　　料　　　[アジェンダ]({{agenda.url}})（各議案の資料はアジェンダ内のリンクからご覧ください）
{{/agenda}}
[右] 以上
