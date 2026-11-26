# Test fixtures

These files are **synthetic** and were written for automated tests. They mimic
the *column layout* of WeChat Pay / Alipay / bank exports but contain no real
account, merchant or personal data. They must never be presented as official
platform exports.

- `wechat_sample.csv` — WeChat-style bill with preamble, refund pair, transfer,
  credit-card repayment and wallet top-up.
- `alipay_sample.csv` — Alipay-style bill including a 余额宝 transfer and a
  refund.
- `bank_sample.csv` — bank statement with separate income/expense columns.
- `generic_unknown.csv` — unknown platform used to test column-mapping prompts.
