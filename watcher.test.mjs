import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { execFileSync } from "node:child_process";

// Import as ESM without a package.json; production still copies watcher.js to watcher.mjs.
const source = await fs.readFile(new URL("./watcher.js", import.meta.url), "utf8");
const watcher = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);

// Public catalog fields captured on 2026-10-17. No account tokens or webhook URLs.
const ID = "af73acc8-ae5b-4521-87b4-d95afd337869";
const SALE = "c645a775-52a3-4fe7-b66b-9deeb0e470a4";
const CURRENCY = "ecd19d3c-7635-402c-a185-eb11cb6c6946";
const IMAGE = "https://xforgeassets002.xboxlive.com/pf-namespace-b63a0803d3653643/13f357d0-33df-4307-9f46-87fa7890740b/SkeletonParty_Thumbnail_0.jpg";
const PRODUCT_URL = `https://www.minecraft.net/en-us/marketplace/pdp/yeggs/skeleton-party/${ID}`;
const START = "2026-10-16T17:00:00.000Z", END = "2026-11-02T18:00:00.000Z";
const NOW = Date.parse("2026-10-17T09:00:00Z");
const legacyKey = `${SALE}|33|${END}`;
const baseItem = {
  id: ID, title: "Skeleton Party", prices: [{ currencyId: CURRENCY, amount: 310 }],
  images: [{ Type: "Thumbnail", Url: IMAGE }],
  rawItem: { Id: ID, Title: { NEUTRAL: "Skeleton Party" }, DisplayProperties: { creatorName: "Yeggs", price: 310, skinPack: { count: 10 } } },
};
const baseSale = { id: SALE, title: "Skeleton Party", discountPercent: 33, startDate: START, endDate: END, items: [baseItem] };
const official = { title: "Skeleton Party", author: "Yeggs", image: IMAGE, url: PRODUCT_URL, price: 310, skinsCount: 10 };
const payload = { sales: { [SALE]: baseSale } };
const blank = () => ({ seenSales: {}, bestDiscounts: {} });
const earlyState = () => ({ seenSales: { [legacyKey]: "2026-10-15T17:14:32.672Z" }, bestDiscounts: { [ID]: 33 } });
const plan = (data = payload, state = blank(), extra = {}) => watcher.planNotifications(data, state, { now: NOW, metadata: async () => official, ...extra });
const field = (embed, name) => embed.fields.find((entry) => entry.name === name)?.value;

test("captured Skeleton promotion includes exact image, creator, product link, reported percentage and unverified equal prices", async () => {
  const state = blank(), before = structuredClone(state);
  const result = await plan(payload, state);
  assert.equal(result.units.length, 1);
  const embed = result.units[0].embed;
  assert.equal(embed.title, "Skeleton Party");
  assert.equal(embed.url, PRODUCT_URL);
  assert.equal(embed.image.url, IMAGE);
  assert.match(embed.description, /Yeggs/);
  assert.match(embed.description, /10 skins/);
  assert.match(field(embed, "Reported promotion"), /33%/);
  assert.match(field(embed, "Price check"), /unverified/);
  assert.match(field(embed, "Available prices"), /Catalog \/ normal: \*\*310 Minecoins/);
  assert.match(field(embed, "Available prices"), /Promotion store: \*\*310 Minecoins/);
  assert.match(field(embed, "Bot history"), /First reported promotion/);
  assert.doesNotMatch(JSON.stringify(embed), /historical best/);
  assert.deepEqual(state, before);
});

test("official API metadata supplies missing cover, creator and full product URL", async () => {
  let calls = 0;
  const data = { sales: [{ ...baseSale, items: [{ id: ID, title: "Skeleton Party" }] }] };
  const result = await plan(data, blank(), { metadata: async (id) => { calls++; assert.equal(id, ID); return official; } });
  assert.equal(calls, 1);
  assert.equal(result.units[0].embed.image.url, IMAGE);
  assert.equal(result.units[0].embed.url, PRODUCT_URL);
  assert.match(result.units[0].embed.description, /Yeggs/);
  assert.match(field(result.units[0].embed, "Available prices"), /Official web listing \(US\): \*\*310/);
});

test("future, ended and invalid promotions never fetch metadata, notify or alter history", async () => {
  for (const [sale, now] of [
    [baseSale, Date.parse(START) - 1], [baseSale, Date.parse(END)],
    [{ ...baseSale, startDate: null }, NOW], [{ ...baseSale, endDate: "invalid" }, NOW],
    [{ ...baseSale, startDate: END, endDate: START }, NOW],
  ]) {
    const state = earlyState(), before = structuredClone(state);
    const result = await plan({ sales: [sale] }, state, { now, metadata: async () => assert.fail("inactive metadata request") });
    assert.equal(result.units.length, 0);
    let writes = 0;
    await watcher.deliverNotifications(result, state, { post: async () => assert.fail("inactive post"), save: async () => writes++ });
    assert.equal(writes, 0);
    assert.deepEqual(state, before);
  }
  assert.equal(watcher.saleWindow(baseSale, Date.parse(START)).active, true);
});

test("an old premature alert is repaired once at the real start, preserving old state", async () => {
  const state = earlyState(), original = structuredClone(state);
  const result = await plan(payload, state);
  assert.equal(result.units.length, 1);
  let saved, posts = 0;
  await watcher.deliverNotifications(result, state, {
    webhook: "https://discord.example/webhook", now: NOW,
    post: async () => posts++, save: async (value) => { saved = structuredClone(value); },
  });
  assert.equal(posts, 1);
  assert.equal(saved.legacySeenSales[legacyKey], original.seenSales[legacyKey]);
  assert.equal(saved.bestDiscounts[ID], 33);
  assert.equal(Object.keys(saved.notifiedItems).length, 1);
  assert.equal((await plan(payload, saved)).units.length, 0);
  assert.deepEqual(state, original);
  const alreadyCorrect = { ...blank(), seenSales: { [legacyKey]: START } };
  assert.equal((await plan(payload, alreadyCorrect)).units.length, 0);
});

test("lower store prices are identified with the actual calculated reduction while keeping reported percent", async () => {
  const data = { sales: [{ ...baseSale, items: [{ ...baseItem, prices: [{ currencyId: CURRENCY, amount: 200 }] }] }] };
  const result = await plan(data);
  assert.match(field(result.units[0].embed, "Price check"), /35% below/);
  assert.match(field(result.units[0].embed, "Reported promotion"), /33%/);
  const unrelatedCurrency = { ...baseItem, prices: [{ currencyId: "some-other-token", amount: 10 }] };
  assert.equal(watcher.getItemInfo(unrelatedCurrency).storePrice, null);
});

test("all items fit Discord limits and only acknowledged batches are checkpointed after a failure", async () => {
  const items = Array.from({ length: 23 }, (_, index) => ({ ...baseItem, id: `${ID}-${index}`, title: `${"Long title ".repeat(35)} ${index}` }));
  const data = { sales: [{ ...baseSale, items }] };
  const result = await plan(data);
  const batches = watcher.batchNotifications(result.units);
  assert.ok(batches.length > 2);
  assert.equal(batches.flat().length, 23);
  for (const batch of batches) {
    assert.ok(batch.length <= 10);
    const embeds = watcher.discordPayload(batch).embeds;
    const size = embeds.reduce((sum, embed) => sum + embed.title.length + embed.description.length + embed.footer.text.length + embed.fields.reduce((n, f) => n + f.name.length + f.value.length, 0), 0);
    assert.ok(size <= 6000);
    for (const embed of embeds) assert.ok(embed.title.length <= 256);
  }
  let calls = 0, saved;
  await assert.rejects(watcher.deliverNotifications(result, blank(), {
    webhook: "https://discord.example/webhook", now: NOW,
    post: async () => { if (++calls === 2) throw new Error("simulated delivery failure"); },
    save: async (state) => { saved = structuredClone(state); },
  }), /simulated delivery failure/);
  assert.equal(Object.keys(saved.notifiedItems).length, batches[0].length);
  assert.equal(saved.seenSales[legacyKey], undefined);
  const remaining = await plan(data, saved);
  assert.equal(remaining.units.length, 23 - batches[0].length);
  await watcher.deliverNotifications(remaining, saved, {
    webhook: "https://discord.example/webhook", now: NOW,
    post: async () => {}, save: async (state) => { saved = structuredClone(state); },
  });
  assert.equal((await plan(data, saved)).units.length, 0);
  assert.ok(saved.seenSales[legacyKey]);
});

test("major-sale pings require both a reported 75% discount and an actual comparable price reduction", async () => {
  const unit = (reported, normal, store) => ({ discount: reported, item: { normalPrice: normal, storePrice: store }, embed: {} });
  const verified = watcher.discordPayload([unit(80, 100, 20)]);
  assert.deepEqual(verified.allowed_mentions, { parse: ["everyone"] });
  assert.match(verified.content, /^@everyone /);
  for (const unqualified of [unit(90, 100, 100), unit(90, null, null), unit(70, 100, 20), unit(80, 100, 30)]) {
    const message = watcher.discordPayload([unqualified]);
    assert.deepEqual(message.allowed_mentions, { parse: [] });
    assert.doesNotMatch(message.content, /@everyone/);
  }
});

test("Discord 429 honors retry_after, requests acknowledgment and prevents unverified promotion pings", async () => {
  const result = await plan();
  const message = watcher.discordPayload(result.units);
  assert.deepEqual(message.allowed_mentions, { parse: [] });
  const waits = [], seen = [];
  await watcher.postDiscord("https://discord.example/webhook?thread_id=123", message, {
    wait: async (ms) => waits.push(ms),
    fetchFn: async (url) => {
      seen.push(url);
      return seen.length === 1
        ? new Response(JSON.stringify({ retry_after: 0.5 }), { status: 429 })
        : new Response(JSON.stringify({ id: "acknowledged" }), { status: 200 });
    },
  });
  assert.deepEqual(waits, [500]);
  assert.equal(seen.length, 2);
  assert.equal(new URL(seen[1]).searchParams.get("wait"), "true");
  assert.equal(new URL(seen[1]).searchParams.get("thread_id"), "123");
});

test("dry-run is offline with a fixture, needs no webhook and cannot modify saved history", async () => {
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), "minecraft-watcher-test-"));
  try {
    const modulePath = path.join(temporary, "watcher.mjs"), fixturePath = path.join(temporary, "fixture.json"), statePath = path.join(temporary, "state.json");
    await fs.writeFile(modulePath, source);
    await fs.writeFile(fixturePath, JSON.stringify({ payload, metadata: { [ID]: official } }));
    const original = JSON.stringify(earlyState());
    await fs.writeFile(statePath, original);
    const env = { ...process.env, DRY_RUN: "true", WATCHER_STATE_FILE: statePath };
    delete env.DISCORD_WEBHOOK_URL;
    const output = execFileSync(process.execPath, [modulePath, "--fixture", fixturePath, "--at", new Date(NOW).toISOString()], { env, encoding: "utf8" });
    const result = JSON.parse(output);
    assert.equal(result.dryRun, true);
    assert.equal(result.messages[0].embeds[0].image.url, IMAGE);
    assert.equal(await fs.readFile(statePath, "utf8"), original);
  } finally { await fs.rm(temporary, { recursive: true, force: true }); }
});

test("corrupt history fails instead of silently resending every promotion", async () => {
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), "minecraft-state-test-"));
  try {
    const stateFile = path.join(temporary, "state.json");
    await fs.writeFile(stateFile, "{corrupt");
    await assert.rejects(watcher.readState(stateFile));
    assert.deepEqual(await watcher.readState(path.join(temporary, "missing.json")), blank());
  } finally { await fs.rm(temporary, { recursive: true, force: true }); }
});
