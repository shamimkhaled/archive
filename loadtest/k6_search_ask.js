// k6 alternative to the Locust file for search and Ask (no Python on the load machine).
//
//   k6 run -e BASE_URL=https://archive-staging.example -e USERNAME=lt_member1 -e PASSWORD=... \
//          -e VUS=100 -e DURATION=10m loadtest/k6_search_ask.js --summary-export loadtest/results/k6.json
//
// Thresholds mirror the acceptance targets; k6 exits non-zero when any is missed.
import http from "k6/http";
import { check, sleep } from "k6";

const BASE = __ENV.BASE_URL || "http://localhost:8000";
const VUS = parseInt(__ENV.VUS || "50", 10);
const DURATION = __ENV.DURATION || "5m";

const QUERIES = [
  "board meeting minutes ICT procurement",
  "loan classification circular",
  "পরিচালনা পর্ষদের সভার কার্যবিবরণী",
  "ঋণ শ্রেণীকরণ নীতিমালা",
  "audit committee recommendations",
  "anti money laundering policy",
];
const QUESTIONS = [
  "What did the board decide about ICT procurement?",
  "Summarise the latest loan classification circular.",
];

export const options = {
  scenarios: {
    search: { executor: "constant-vus", vus: VUS, duration: DURATION, exec: "search" },
    ask: { executor: "constant-vus", vus: Math.max(1, Math.floor(VUS / 5)), duration: DURATION, exec: "ask" },
  },
  thresholds: {
    "http_req_duration{op:search}": ["p(95)<1500"],
    "http_req_duration{op:ask}": ["p(95)<8000"],
    "http_req_failed{op:search}": ["rate<0.01"],
  },
};

export function setup() {
  const res = http.post(`${BASE}/token`, { username: __ENV.USERNAME, password: __ENV.PASSWORD });
  check(res, { "login ok": (r) => r.status === 200 });
  return { headers: { Authorization: `Bearer ${res.json("access_token")}` } };
}

export function search(ctx) {
  const q = encodeURIComponent(QUERIES[Math.floor(Math.random() * QUERIES.length)]);
  const res = http.get(`${BASE}/api/search?q=${q}`, { headers: ctx.headers, tags: { op: "search" } });
  check(res, { "search 200": (r) => r.status === 200 });
  sleep(1 + Math.random() * 3);
}

export function ask(ctx) {
  const body = JSON.stringify({ question: QUESTIONS[Math.floor(Math.random() * QUESTIONS.length)] });
  const res = http.post(`${BASE}/api/v1/ask`, body, {
    headers: { ...ctx.headers, "Content-Type": "application/json" },
    tags: { op: "ask" },
    timeout: "60s",
  });
  check(res, { "ask 200 or 429": (r) => r.status === 200 || r.status === 429 });
  sleep(5 + Math.random() * 10);
}
