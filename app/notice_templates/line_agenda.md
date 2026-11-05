---
name: アジェンダの配信（LINE）
channel: line
purpose: agenda
mention: all
is_default: true
---
{{#late}}アジェンダの配信が遅くなり、申し訳ありません。
{{/late}}{{event.date}}の{{event.title}}のアジェンダをお送りします。

【{{event.title}}】
日時：{{event.date}} {{event.time}}
場所：{{?event.place}}
{{#online}}オンライン：{{event.online_url}}
{{/online}}
{{#agenda}}■アジェンダ（各議案の資料はアジェンダ内のリンクからご覧ください）
{{agenda.url}}

{{/agenda}}{{#opinion}}■意見書
{{opinion.url}}

{{/opinion}}よろしくお願いいたします。
