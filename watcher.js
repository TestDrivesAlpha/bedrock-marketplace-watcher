import fs from "node:fs/promises";

const API = "http://127.0.0.1:3000";
const API_USER = process.env.CATALOG_API_USER || "admin";
const API_PASS =
  process.env.CATALOG_API_PASS || "github-actions-local-password";

const WEBHOOK = process.env.DISCORD_WEBHOOK_URL;

const MIN_DISCOUNT = Number(process.env.MIN_DISCOUNT || 1);
const STATE_FILE = "watcher-state.json";

if (!WEBHOOK) {
  throw new Error("DISCORD_WEBHOOK_URL is missing.");
}

async function readState() {
  try {
    return JSON.parse(await fs.readFile(STATE_FILE, "utf8"));
  } catch {
    return {
      seenSales: {},
      bestDiscounts: {},
    };
  }
}

async function saveState(state) {
  await fs.writeFile(STATE_FILE, JSON.stringify(state, null, 2));
}

async function request(url, options = {}) {
  const res = await fetch(url, options);
  const text = await res.text();

  let json = null;

  try {
    json = JSON.parse(text);
  } catch {}

  return {
    ok: res.ok,
    status: res.status,
    text,
    json,
  };
}

async function login() {
  const result = await request(`${API}/login`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      username: API_USER,
      password: API_PASS,
    }),
  });

  if (!result.ok) {
    throw new Error(
      `Marketplace API login failed (${result.status}): ${result.text}`
    );
  }

  const token =
    result.json?.token ||
    result.json?.accessToken ||
    result.json?.jwt;

  if (!token) {
    throw new Error("Marketplace API did not return a login token.");
  }

  return token;
}

async function getSales(token) {
  const endpoints = [
    "/marketplace/sales/prod",
    "/marketplace/sales",
  ];

  for (const endpoint of endpoints) {
    const result = await request(`${API}${endpoint}`, {
      headers: {
        Authorization: `Bearer ${token}`,
      },
    });

    if (result.ok) {
      return result.json;
    }

    if (
      result.text
        .toLowerCase()
        .includes("no sales currently active")
    ) {
      return null;
    }

    if (result.status !== 404) {
      throw new Error(
        `Marketplace sales request failed (${result.status}): ${result.text}`
      );
    }
  }

  throw new Error("Could not find a usable Marketplace sales endpoint.");
}

function getSalesArray(payload) {
  if (!payload) return [];

  if (Array.isArray(payload)) {
    return payload;
  }

  if (Array.isArray(payload.sales)) {
    return payload.sales;
  }

  if (
    payload.sales &&
    typeof payload.sales === "object"
  ) {
    return Object.entries(payload.sales).map(
      ([storeKey, sale]) => ({
        ...sale,
        __storeKey: storeKey,
      })
    );
  }

  if (Array.isArray(payload.data)) {
    return payload.data;
  }

  const possibleSales = [];

  for (const [key, value] of Object.entries(payload)) {
    if (!value || typeof value !== "object") continue;

    if (
      "discountPercent" in value ||
      "discountPercentage" in value ||
      "discount" in value
    ) {
      possibleSales.push({
        ...value,
        __storeKey: key,
      });
    }
  }

  return possibleSales;
}

function getDiscount(sale) {
  const raw =
    sale.discountPercent ??
    sale.discountPercentage ??
    sale.discount ??
    0;

  const number = parseFloat(String(raw).replace("%", ""));

  return Number.isFinite(number) ? number : 0;
}

function getItems(sale) {
  for (const key of [
    "items",
    "products",
    "entries",
    "offers",
  ]) {
    if (Array.isArray(sale[key])) {
      return sale[key];
    }
  }

  return [];
}

function getItemInfo(item) {
  if (typeof item === "string") {
    return {
      id: item,
      title: item,
    };
  }

  const id =
    item?.id ??
    item?.itemId ??
    item?.ItemId ??
    item?.Id ??
    item?.catalogItemId ??
    null;

  const title =
    item?.title ??
    item?.name ??
    item?.displayName ??
    item?.Title ??
    item?.Name ??
    id ??
    "Unknown Marketplace item";

  return {
    id: String(id ?? title),
    title: String(title),
  };
}

function saleIdentity(sale, discount) {
  const id =
    sale.id ??
    sale.storeId ??
    sale.saleId ??
    sale.__storeKey ??
    sale.title ??
    sale.name ??
    "sale";

  const end =
    sale.endDate ??
    sale.end ??
    sale.endTime ??
    "unknown-end";

  return `${id}|${discount}|${end}`;
}

function discordTime(date) {
  if (!date) return null;

  const time = Date.parse(date);

  if (!Number.isFinite(time)) {
    return String(date);
  }

  const seconds = Math.floor(time / 1000);

  return `<t:${seconds}:F> (<t:${seconds}:R>)`;
}

async function sendDiscord(sales) {
  const embeds = sales.slice(0, 10).map((entry) => {
    const sale = entry.sale;

    const title =
      sale.title ??
      sale.name ??
      "Bedrock Marketplace Sale";

    const itemNames = entry.items
      .slice(0, 15)
      .map((item) => `• ${item.title}`);

    let description = "";

    if (itemNames.length) {
      description = itemNames.join("\n");

      if (entry.items.length > 15) {
        description += `\n…and ${
          entry.items.length - 15
        } more`;
      }
    } else {
      description =
        "A new Marketplace discount is active.";
    }

    const fields = [
      {
        name: "Discount",
        value: `🔥 **${entry.discount}% OFF**`,
        inline: true,
      },
      {
        name: "Items",
        value: String(entry.items.length || "Unknown"),
        inline: true,
      },
    ];

    if (entry.newBestCount > 0) {
      fields.push({
        name: "New historical best",
        value: `📉 ${entry.newBestCount} item${
          entry.newBestCount === 1 ? "" : "s"
        }`,
        inline: true,
      });
    }

    const end =
      sale.endDate ??
      sale.end ??
      sale.endTime ??
      null;

    if (end) {
      fields.push({
        name: "Ends",
        value: discordTime(end),
        inline: false,
      });
    }

    return {
      title: `🔥 ${title}`,
      description: description.slice(0, 4000),
      fields,
    };
  });

  const highest = Math.max(
    ...sales.map((x) => x.discount)
  );

  const payload = {
    content:
      highest >= 75
        ? `@everyone 🚨 **BEDROCK MARKETPLACE: ${highest}% OFF SALE DETECTED!**`
        : `🔥 **New Bedrock Marketplace discount detected! Up to ${highest}% off.**`,
    embeds,
    allowed_mentions: {
      parse: highest >= 75 ? ["everyone"] : [],
    },
  };

  const res = await fetch(WEBHOOK, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });

  if (!res.ok) {
    throw new Error(
      `Discord webhook failed (${res.status}): ${await res.text()}`
    );
  }
}

const state = await readState();
const token = await login();
const payload = await getSales(token);
const sales = getSalesArray(payload);

if (sales.length === 0) {
  console.log("No active Marketplace sales right now.");
  process.exit(0);
}

const newSales = [];

for (const sale of sales) {
  const discount = getDiscount(sale);

  if (discount < MIN_DISCOUNT) {
    continue;
  }

  const items = getItems(sale).map(getItemInfo);
  const key = saleIdentity(sale, discount);

  let newBestCount = 0;

  for (const item of items) {
    const previous =
      Number(state.bestDiscounts[item.id] || 0);

    if (discount > previous) {
      state.bestDiscounts[item.id] = discount;
      newBestCount++;
    }
  }

  if (!state.seenSales[key]) {
    newSales.push({
      sale,
      discount,
      items,
      newBestCount,
    });

    state.seenSales[key] =
      new Date().toISOString();
  }
}

/* Remove very old remembered sale IDs. */
const cutoff =
  Date.now() - 120 * 24 * 60 * 60 * 1000;

for (const [key, date] of Object.entries(
  state.seenSales
)) {
  const time = Date.parse(date);

  if (Number.isFinite(time) && time < cutoff) {
    delete state.seenSales[key];
  }
}

if (newSales.length === 0) {
  console.log(
    "Sales exist, but they have already been reported."
  );

  await saveState(state);
  process.exit(0);
}

newSales.sort(
  (a, b) => b.discount - a.discount
);

await sendDiscord(newSales);
await saveState(state);

console.log(
  `Discord notified about ${newSales.length} new Marketplace sale(s).`
);
