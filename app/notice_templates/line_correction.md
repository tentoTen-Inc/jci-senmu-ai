---
name: 修正の再案内（LINE）
channel: line
purpose: correction
mention: all
---
【修正】{{event.title}}のご案内に修正があります。お手数ですが、こちらをご覧ください。

【{{event.title}}】
日時：{{event.date}} {{event.time}}
場所：{{?event.place}}
{{#online}}オンライン：{{event.online_url}}
{{/online}}
{{#agenda}}■アジェンダ
{{agenda.url}}

{{/agenda}}よろしくお願いいたします。
