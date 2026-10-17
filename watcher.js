import fs from "node:fs/promises";
import { pathToFileURL } from "node:url";

const API = process.env.CATALOG_API_URL || "http://127.0.0.1:3000";
const STATE_FILE = process.env.WATCHER_STATE_FILE || "watcher-state.json";
const OFFICIAL_ITEM_API = "https://net-secondary.web.minecraft-services.net/api/v1.0/en-us/marketplace/item/";
const MINECOIN_IDS = new Set([
  "ecd19d3c-7635-402c-a185-eb11cb6c6946",
  "0113e233-7637-48e7-91b0-349fdc74713d",
  "mc", "minecoin", "minecoins",
]);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const numberOrNull = (value) => value !== null && value !== undefined && value !== "" && Number.isFinite(Number(value)) ? Number(value) : null;
const shorten = (value, max) => String(value ?? "").slice(0, max);

export async function readState(file = STATE_FILE) {
  try {
    const state = JSON.parse(await fs.readFile(file, "utf8"));
    if (!state || !state.seenSales || !state.bestDiscounts) throw new Error("Invalid watcher state.");
    return state;
  } catch (error) {
    // An unreadable history must not silently become a fresh history and resend everything.
    if (error.code !== "ENOENT") throw error;
    return { seenSales: {}, bestDiscounts: {} };
  }
}

export async function saveState(state, file = STATE_FILE) {
  const temporary = `${file}.${process.pid}.tmp`;
  await fs.writeFile(temporary, `${JSON.stringify(state, null, 2)}\n`);
  await fs.rename(temporary, file);
}

async function request(url, options = {}, fetchFn = fetch) {
  const response = await fetchFn(url, { ...options, signal: options.signal || AbortSignal.timeout(20_000) });
  const text = await response.text();
  let json = null;
  try { json = JSON.parse(text); } catch {}
  return { ok: response.ok, status: response.status, headers: response.headers, text, json };
}

async function getSales() {
  const login = await request(`${API}/login`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      username: process.env.CATALOG_API_USER || "admin",
      password: process.env.CATALOG_API_PASS || "github-actions-local-password",
    }),
  });
  if (!login.ok) throw new Error(`Marketplace reader login failed (${login.status}).`);
  const token = login.json?.token || login.json?.accessToken || login.json?.jwt;
  if (!token) throw new Error("Marketplace reader did not return a login token.");
  for (const endpoint of ["/marketplace/sales/prod", "/marketplace/sales"]) {
    const result = await request(`${API}${endpoint}`, { headers: { Authorization: `Bearer ${token}` } });
    if (result.ok) return result.json;
    if (result.text.toLowerCase().includes("no sales currently active")) return null;
    if (result.status !== 404) throw new Error(`Marketplace sales request failed (${result.status}).`);
  }
  throw new Error("Could not find a usable Marketplace sales endpoint.");
}

export function getSalesArray(payload) {
  if (!payload) return [];
  if (Array.isArray(payload)) return payload;
  if (Array.isArray(payload.sales)) return payload.sales;
  if (payload.sales && typeof payload.sales === "object") {
    return Object.entries(payload.sales).map(([key, sale]) => ({ ...sale, __storeKey: key }));
  }
  if (Array.isArray(payload.data)) return payload.data;
  return Object.entries(payload).filter(([, value]) => value && typeof value === "object" &&
    ["discountPercent", "discountPercentage", "discount"].some((key) => key in value))
    .map(([key, sale]) => ({ ...sale, __storeKey: key }));
}

export function getDiscount(sale) {
  const raw = sale.discountPercent ?? sale.discountPercentage ?? sale.discount;
  const value = parseFloat(String(raw ?? "").replace("%", ""));
  // This API exposes percentage points; do not guess whether an arbitrary fraction means a percentage.
  return Number.isFinite(value) && value > 0 && value <= 100 ? value : 0;
}

export function saleWindow(sale, now = Date.now()) {
  const start = Date.parse(sale.startDate ?? sale.start ?? sale.startTime ?? "");
  const end = Date.parse(sale.endDate ?? sale.end ?? sale.endTime ?? "");
  if (!Number.isFinite(start) || !Number.isFinite(end) || start >= end) return { active: false, reason: "invalid or missing promotion dates" };
  return { start, end, active: start <= now && now < end, reason: now < start ? "not started" : "ended" };
}

function getItems(sale) {
  for (const key of ["items", "products", "entries", "offers"]) if (Array.isArray(sale[key])) return sale[key];
  return [];
}

export function saleIdentity(sale, discount) {
  const id = sale.id ?? sale.storeId ?? sale.saleId ?? sale.__storeKey ?? sale.title ?? sale.name ?? "sale";
  const end = sale.endDate ?? sale.end ?? sale.endTime ?? "unknown-end";
  // Keep the previous key format for compatibility with existing history.
  return `${id}|${discount}|${end}`;
}

function safeHttps(value) {
  try { const url = new URL(value); return url.protocol === "https:" ? url.href : null; } catch { return null; }
}

function officialProductUrl(value, id) {
  const url = safeHttps(value);
  if (url && new URL(url).hostname === "www.minecraft.net") return url;
  return id ? `https://www.minecraft.net/en-us/marketplace/pdp?id=${encodeURIComponent(id)}` : null;
}

function titleOf(value) {
  if (typeof value === "string") return value;
  return value?.NEUTRAL ?? value?.neutral ?? value?.["en-US"] ?? null;
}

function minecoinPrice(value) {
  if (!value) return null;
  if (Array.isArray(value)) {
    for (const entry of value) {
      const currency = String(entry?.currencyId ?? entry?.CurrencyId ?? entry?.Id ?? "").toLowerCase();
      if (MINECOIN_IDS.has(currency)) {
        const amount = numberOrNull(entry.amount ?? entry.Amount);
        if (amount !== null && amount >= 0) return amount;
      }
      const nested = minecoinPrice(entry?.Amounts ?? entry?.amounts);
      if (nested !== null) return nested;
    }
    return null;
  }
  for (const [key, amount] of Object.entries(value)) {
    if (MINECOIN_IDS.has(key.toLowerCase()) && numberOrNull(amount) !== null) return Number(amount);
  }
  return minecoinPrice(value.Prices ?? value.prices);
}

function imageOf(images) {
  if (!Array.isArray(images)) return null;
  const ordered = [...images].sort((a, b) => Number(/thumbnail/i.test(b?.Type ?? b?.type ?? b?.Tag ?? "")) - Number(/thumbnail/i.test(a?.Type ?? a?.type ?? a?.Tag ?? "")));
  for (const image of ordered) {
    const url = safeHttps(typeof image === "string" ? image : image?.Url ?? image?.url);
    if (url) return url;
  }
  return null;
}

export function getItemInfo(item, official = {}) {
  if (typeof item === "string") item = { id: item };
  const raw = item?.rawItem ?? item ?? {};
  const display = raw.DisplayProperties ?? item?.displayProperties ?? {};
  const id = item?.id ?? item?.itemId ?? item?.ItemId ?? item?.Id ?? item?.catalogItemId ?? raw.Id ?? null;
  const title = titleOf(item?.title ?? item?.name ?? item?.displayName ?? item?.Title ?? raw.Title) ?? official.title ?? official.neutralTitle ?? id ?? "Unknown Marketplace item";
  const creator = item?.creatorName ?? display.creatorName ?? official.author ?? null;
  const normalPrice = numberOrNull(item?.normalPrice ?? display.price) ?? minecoinPrice(raw.Price ?? raw.PriceOptions) ?? minecoinPrice(item?.virtualCurrencyPrices);
  const storePrice = minecoinPrice(item?.prices ?? item?.storePrice);
  return {
    id: id ? String(id) : null, title: String(title), creator: creator ? String(creator) : null,
    image: imageOf(item?.images ?? raw.Images) ?? safeHttps(official.image) ?? imageOf(official.images),
    url: officialProductUrl(official.url ?? item?.url, id),
    normalPrice, storePrice, officialPrice: numberOrNull(official.price),
    skinCount: numberOrNull(display.skinPack?.count ?? official.skinsCount),
  };
}

export async function fetchOfficialMetadata(id, fetchFn = fetch) {
  if (!id) return {};
  const response = await request(`${OFFICIAL_ITEM_API}${encodeURIComponent(id)}?geography=US`, {
    signal: AbortSignal.timeout(8_000),
    headers: {
      Accept: "application/json",
      "User-Agent": "BedrockMarketplaceWatcher/2.0 (+https://github.com/TestDrivesAlpha/bedrock-marketplace-watcher)",
      Origin: "https://www.minecraft.net",
      Referer: "https://www.minecraft.net/",
    },
  }, fetchFn);
  if (!response.ok || !response.json?.result) throw new Error(`Official item metadata unavailable (${response.status}).`);
  return response.json.result;
}

export function discordTime(milliseconds) {
  const seconds = Math.floor(milliseconds / 1000);
  return `<t:${seconds}:F> (<t:${seconds}:R>)`;
}

export function buildEmbed(unit, state) {
  const { item, discount, sale, window } = unit;
  const lowerPrice = item.normalPrice !== null && item.storePrice !== null && item.storePrice < item.normalPrice;
  const priceReduction = lowerPrice ? Math.round((1 - item.storePrice / item.normalPrice) * 100) : null;
  const priceLines = [];
  if (item.normalPrice !== null) priceLines.push(`Catalog / normal: **${item.normalPrice} Minecoins**`);
  if (item.storePrice !== null) priceLines.push(`Promotion store: **${item.storePrice} Minecoins**`);
  if (item.officialPrice !== null) priceLines.push(`Official web listing (US): **${item.officialPrice} Minecoins**`);
  const fields = [
    { name: "Reported promotion", value: `**${discount}% off**`, inline: true },
    { name: "Price check", value: lowerPrice ? `Catalog store price is ${priceReduction}% below catalog normal price.` : "**Discounted purchase price unverified.** The catalog does not show a lower store price.", inline: false },
    { name: "Available prices", value: priceLines.join("\n") || "No comparable prices supplied by the source.", inline: false },
    { name: "Starts", value: discordTime(window.start), inline: false },
    { name: "Ends", value: discordTime(window.end), inline: false },
  ];
  const previous = numberOrNull(state.bestDiscounts?.[item.id]);
  if (previous === null) fields.push({ name: "Bot history", value: "First reported promotion observed by this bot.", inline: false });
  else if (discount > previous) fields.push({ name: "Bot history", value: `New best reported discount observed by this bot (previous: ${previous}%).`, inline: false });
  const description = [
    item.creator ? `By **${shorten(item.creator, 150)}**` : "Creator unavailable from the source.",
    item.skinCount > 0 ? `${item.skinCount} skins` : null,
    `[Open the official Marketplace item](${item.url})`,
    "Promotion details come from the catalog. Check your in-game item page for the price available to your account.",
  ].filter(Boolean).join("\n");
  const embed = {
    title: shorten(item.title, 256), url: item.url, description: shorten(description, 1800),
    fields, color: lowerPrice ? 0x35a75a : 0xe7a93b,
    footer: { text: shorten(`Promotion: ${sale.title ?? sale.name ?? "Minecraft Marketplace"} | Item: ${item.id}`, 240) },
  };
  if (item.image) embed.image = { url: item.image };
  return embed;
}

export async function planNotifications(payload, state, { now = Date.now(), minDiscount = 1, metadata = fetchOfficialMetadata } = {}) {
  const units = [], skipped = [], metadataCache = new Map();
  const legacy = state.legacySeenSales ?? (state.schemaVersion === 2 ? {} : state.seenSales ?? {});
  const notified = state.notifiedItems ?? {};
  for (const sale of getSalesArray(payload)) {
    const window = saleWindow(sale, now);
    // Date eligibility is checked before metadata calls and all history updates.
    if (!window.active) { skipped.push({ sale: sale.title ?? sale.id, reason: window.reason }); continue; }
    const discount = getDiscount(sale);
    if (discount < minDiscount || !discount) continue;
    const saleKey = saleIdentity(sale, discount);
    const legacyTime = Date.parse(legacy[saleKey] ?? "");
    // Old alerts sent before the documented start must not suppress the real start alert.
    if (Number.isFinite(legacyTime) && legacyTime >= window.start && legacyTime < window.end) continue;
    const saleUnits = [];
    for (const rawItem of getItems(sale)) {
      const basic = getItemInfo(rawItem);
      if (!basic.id) { skipped.push({ sale: sale.title ?? sale.id, reason: "item has no stable ID" }); continue; }
      const key = `${saleKey}|${window.start}|${basic.id}`;
      if (notified[key]) continue;
      if (!metadataCache.has(basic.id)) {
        metadataCache.set(basic.id, Promise.resolve().then(() => metadata(basic.id)).catch(() => ({})));
      }
      const item = getItemInfo(rawItem, await metadataCache.get(basic.id));
      const unit = { key, saleKey, sale, item, discount, window };
      unit.embed = buildEmbed(unit, state);
      saleUnits.push(unit);
    }
    if (!getItems(sale).length) skipped.push({ sale: sale.title ?? sale.id, reason: "promotion has no identifiable items" });
    units.push(...saleUnits);
  }
  // Deduplicate repeated references before delivery and keep the highest reported promotions first.
  return { units: [...new Map(units.map((unit) => [unit.key, unit])).values()].sort((a, b) => b.discount - a.discount), skipped };
}

function embedLength(embed) {
  return (embed.title?.length ?? 0) + (embed.description?.length ?? 0) + (embed.footer?.text?.length ?? 0) +
    (embed.author?.name?.length ?? 0) + (embed.fields ?? []).reduce((sum, field) => sum + field.name.length + field.value.length, 0);
}

export function batchNotifications(units) {
  const batches = [];
  let current = [], length = 0;
  for (const unit of units) {
    const size = embedLength(unit.embed);
    if (size > 6000) throw new Error("One Marketplace embed exceeds Discord's size limit.");
    if (current.length && (current.length === 10 || length + size > 6000)) { batches.push(current); current = []; length = 0; }
    current.push(unit); length += size;
  }
  if (current.length) batches.push(current);
  return batches;
}

export function discordPayload(batch) {
  const highest = Math.max(...batch.map((unit) => unit.discount));
  const verifiedMajorDiscount = batch.some(({ discount, item }) =>
    discount >= 75 && item.normalPrice > 0 && item.storePrice !== null &&
    item.storePrice < item.normalPrice && (1 - item.storePrice / item.normalPrice) * 100 >= 75);
  return {
    content: `${verifiedMajorDiscount ? "@everyone " : ""}Minecraft Marketplace promotion detected. Highest reported discount: **${highest}% off**. See each item's price check below.`,
    embeds: batch.map((unit) => unit.embed),
    // Preserve major-sale alerts only when both source percentage and comparable prices qualify.
    allowed_mentions: { parse: verifiedMajorDiscount ? ["everyone"] : [] },
  };
}

export async function postDiscord(webhook, payload, { fetchFn = fetch, wait = sleep } = {}) {
  const url = new URL(webhook);
  url.searchParams.set("wait", "true");
  for (let attempt = 0; attempt < 5; attempt++) {
    let response;
    try {
      response = await request(url.href, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }, fetchFn);
    } catch {
      // No acknowledgement: leave this batch pending for the next run.
      throw new Error("Discord delivery was not acknowledged; this batch remains pending.");
    }
    if (response.ok) return;
    if (response.status === 429 && attempt < 4) {
      const seconds = numberOrNull(response.json?.retry_after) ?? numberOrNull(response.headers?.get?.("retry-after")) ?? 1;
      // A long rate limit is retried by the next scheduled run without dropping notifications.
      if (seconds > 60) throw new Error("Discord is rate limited; this batch remains pending for the next run.");
      await wait(Math.max(250, seconds * 1000));
      continue;
    }
    if (response.status >= 500 && attempt < 4) { await wait(1000 * (attempt + 1)); continue; }
    throw new Error(`Discord webhook failed (${response.status}); this batch remains pending.`);
  }
}

export async function deliverNotifications(plan, originalState, { webhook, dryRun = false, save = saveState, post = postDiscord, now = Date.now() } = {}) {
  const batches = batchNotifications(plan.units);
  if (dryRun) return { dryRun: true, messages: batches.map(discordPayload), skipped: plan.skipped };
  if (!batches.length) return { sentItems: 0, messages: 0, skipped: plan.skipped };
  if (!webhook) throw new Error("DISCORD_WEBHOOK_URL is missing. Use --dry-run to preview without posting.");
  const state = structuredClone(originalState);
  state.legacySeenSales ??= state.schemaVersion === 2 ? {} : { ...state.seenSales };
  state.schemaVersion = 2;
  state.notifiedItems ??= {};
  for (const batch of batches) {
    await post(webhook, discordPayload(batch));
    const timestamp = new Date(now).toISOString();
    for (const unit of batch) {
      state.notifiedItems[unit.key] = timestamp;
      state.bestDiscounts[unit.item.id] = Math.max(Number(state.bestDiscounts[unit.item.id] || 0), unit.discount);
    }
    for (const unit of batch) {
      if (plan.units.filter((candidate) => candidate.saleKey === unit.saleKey).every((candidate) => state.notifiedItems[candidate.key])) state.seenSales[unit.saleKey] = timestamp;
    }
    // Checkpoint only acknowledged batches; later failures leave all remaining items pending.
    await save(state);
  }
  return { sentItems: plan.units.length, messages: batches.length, skipped: plan.skipped };
}

export async function main(args = process.argv.slice(2)) {
  const dryRun = args.includes("--dry-run") || /^(1|true)$/i.test(process.env.DRY_RUN || "");
  const option = (name) => { const index = args.indexOf(name); return index < 0 ? null : args[index + 1]; };
  const fixture = option("--fixture");
  if ((fixture || option("--at")) && !dryRun) throw new Error("--fixture and --at are allowed only with --dry-run.");
  if (!dryRun && !process.env.DISCORD_WEBHOOK_URL) throw new Error("DISCORD_WEBHOOK_URL is missing. Use --dry-run to preview without posting.");
  const now = option("--at") ? Date.parse(option("--at")) : Date.now();
  if (!Number.isFinite(now)) throw new Error("Invalid --at timestamp.");
  const minDiscount = Number(process.env.MIN_DISCOUNT ?? 1);
  if (!Number.isFinite(minDiscount) || minDiscount < 0 || minDiscount > 100) throw new Error("MIN_DISCOUNT must be between 0 and 100.");
  const fixtureData = fixture ? JSON.parse(await fs.readFile(fixture, "utf8")) : null;
  const payload = fixtureData ? fixtureData.payload ?? fixtureData : await getSales();
  const state = await readState();
  const options = { now, minDiscount };
  // An offline fixture never causes an HTTP request, including metadata enrichment.
  if (fixtureData) options.metadata = async (id) => fixtureData.metadata?.[id] ?? {};
  const plan = await planNotifications(payload, state, options);
  const result = await deliverNotifications(plan, state, { webhook: process.env.DISCORD_WEBHOOK_URL, dryRun, now });
  console.log(JSON.stringify(result, null, 2));
  return result;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => { console.error(error.message); process.exitCode = 1; });
}
