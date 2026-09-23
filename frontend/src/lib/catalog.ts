import type { Locale } from "./types";

type Localized<T> = Record<string, T> & { ru: T; kk: T };

export type Scenario = {
  scenario_id: string;
  slug: string;
  name: string;
  description: string;
  domain: string;
  category: string;
  priority: "normal" | "high" | "urgent";
  not_this_if: { condition: string; use_instead: string }[];
  requires_identification: boolean;
  requires_confirmation: boolean;
  slots: { required: string[]; optional: string[] };
  actions: string[];
  handoff: Record<string, unknown> | null;
  examples: Localized<string[]>;
  responses: Record<string, unknown>;
};
export type SystemIntent = {
  id: string;
  description: string;
  behavior: string;
  response: Localized<string>;
};
export type CatalogData = { scenarios: Scenario[]; systemIntents: SystemIntent[] };

function object(value: unknown, field: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(`Invalid catalog field: ${field}`);
  return value as Record<string, unknown>;
}
function string(value: unknown, field: string): string {
  if (typeof value !== "string") throw new Error(`Invalid catalog field: ${field}`);
  return value;
}
function strings(value: unknown, field: string): string[] {
  if (!Array.isArray(value) || value.some(item => typeof item !== "string")) throw new Error(`Invalid catalog field: ${field}`);
  return value;
}
function boolean(value: unknown, field: string): boolean {
  if (typeof value !== "boolean") throw new Error(`Invalid catalog field: ${field}`);
  return value;
}
function scenario(value: unknown): Scenario {
  const row = object(value, "scenario");
  const priority = string(row.priority, "priority");
  if (priority !== "normal" && priority !== "high" && priority !== "urgent") throw new Error("Invalid catalog field: priority");
  const slots = object(row.slots === undefined ? {} : row.slots, "slots");
  const exampleValues = object(row.examples === undefined ? {} : row.examples, "examples");
  const examples: Localized<string[]> = { ru: [], kk: [] };
  for (const [language, phrases] of Object.entries(exampleValues)) examples[language] = strings(phrases, `examples.${language}`);
  const boundaries = row.not_this_if === undefined ? [] : row.not_this_if;
  if (!Array.isArray(boundaries)) throw new Error("Invalid catalog field: not_this_if");
  return {
    ...row,
    scenario_id: string(row.scenario_id, "scenario_id"),
    slug: string(row.slug, "slug"),
    name: string(row.name, "name"),
    description: string(row.description, "description"),
    domain: string(row.domain, "domain"),
    category: string(row.category, "category"),
    priority,
    not_this_if: boundaries.map(value => {
      const boundary = object(value, "not_this_if[]");
      return { ...boundary, condition: string(boundary.condition, "condition"), use_instead: string(boundary.use_instead, "use_instead") };
    }),
    requires_identification: row.requires_identification === undefined ? false : boolean(row.requires_identification, "requires_identification"),
    requires_confirmation: row.requires_confirmation === undefined ? false : boolean(row.requires_confirmation, "requires_confirmation"),
    slots: { ...slots, required: strings(slots.required === undefined ? [] : slots.required, "slots.required"), optional: strings(slots.optional === undefined ? [] : slots.optional, "slots.optional") },
    actions: strings(row.actions === undefined ? [] : row.actions, "actions"),
    handoff: row.handoff === undefined || row.handoff === null ? null : object(row.handoff, "handoff"),
    examples,
    responses: object(row.responses === undefined ? {} : row.responses, "responses"),
  };
}
function systemIntent(value: unknown): SystemIntent {
  const row = object(value, "system_intent");
  const responseValues = object(row.response === undefined ? {} : row.response, "response");
  const response: Localized<string> = { ru: "", kk: "" };
  for (const [language, text] of Object.entries(responseValues)) response[language] = string(text, `response.${language}`);
  return { ...row, id: string(row.id, "id"), description: string(row.description, "description"), behavior: string(row.behavior, "behavior"), response };
}

// The /kit endpoints return raw stored payloads, so optional model defaults may be absent.
export function normalizeCatalog(scenarios: unknown, systemIntents: unknown): CatalogData {
  if (!Array.isArray(scenarios) || !Array.isArray(systemIntents)) throw new Error("Invalid catalog collection");
  return { scenarios: scenarios.map(scenario), systemIntents: systemIntents.map(systemIntent) };
}

export const domains: Record<string, [string, string]> = {
  auto: ["Автострахование", "Көлік"], property: ["Имущество", "Мүлік"], health: ["Здоровье", "Денсаулық"],
  travel: ["Поездки", "Сапарлар"], accident: ["Несчастные случаи", "Жазатайым оқиғалар"], corporate: ["Для бизнеса", "Бизнес"], general: ["Общие вопросы", "Жалпы сұрақтар"],
};
export function domainTitle(domain: string, locale: Locale) { return domains[domain]?.[locale === "ru" ? 0 : 1] ?? domain; }
