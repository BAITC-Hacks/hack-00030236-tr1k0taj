import type { Conversation, Fact, Turn } from "./types";

// Explicitly selected UI examples, never used to answer arbitrary user input.
// Facts are minimal projections of datasets/mock_backend.json and knowledge_base.json.
const claim: Fact = { key: "claim.status", value: "Для продолжения рассмотрения нужен акт управляющей компании.", source: "get_claim", source_id: "CL-500311", origin: "required" };
const document: Fact = { key: "claim.document_submission", value: "Фото документа принимаются. Отправьте его через приложение или на claims@saqta-insurance.example, указав номер обращения.", source: "kb_lookup", source_id: "claims.submission", origin: "background", usedInTurn: 3 };
function turn(id: number, text: string, answer: string, scenario: string, reason: string, facts: Fact[] = []): Turn {
  return { id, text, answer, language: "ru", channel: "text", scenarios: [{ scenario_id: scenario, reason }], alternatives: [], slots: {}, facts, timings: {} };
}
export function makeExample(kind: "claim" | "payment"): Conversation {
  const base: Conversation = { id: `example-${kind}`, generation: 0, startedAt: "2026-10-01T10:00:00+05:00", endedAt: "2026-10-01T10:02:00+05:00", mode: "example", turns: [], facts: [], pendingTopics: [], helper: "disabled", outcome: "completed" };
  if (kind === "payment") {
    const payment: Fact = { key: "payment.status", value: "31 200 ₸ списаны 30.09.2026. Полис не выпущен.", source: "check_payment", source_id: "P-3001", origin: "required" };
    return { ...base, outcome: "handoff", client: { name: "Yerlan Omarov", phone: "+7 ••• ••• •• 03", city: "Шымкент" }, facts: [payment], turns: [turn(1, "Деньги за страховку списали вчера, а полис не пришёл.", "Платёж найден, но полис не выпущен. Этот вопрос нужно передать оператору вместе с данными платежа.", "SC30", "Есть списание, но отсутствует выпущенный полис. Нужна проверка оператором.", [payment])] };
  }
  return { ...base, helper: "ready", client: { name: "Natalia Smirnova", phone: "+7 ••• ••• •• 04", city: "Алматы" }, claim: { id: "CL-500311", status: "documents_requested", nextStep: "Предоставить акт управляющей компании" }, facts: [claim, document], turns: [
    turn(1, "Что с моим заявлением по затоплению?", "Помогу проверить статус. Назовите, пожалуйста, номер телефона, указанный в обращении.", "SC17", "Клиент спрашивает о ходе рассмотрения существующего обращения."),
    turn(2, "+7 ••• ••• •• 04", "По обращению CL-500311 ожидается акт управляющей компании. После его получения рассмотрение можно продолжить.", "SC17", "Продолжение вопроса о статусе обращения после идентификации.", [claim]),
    turn(3, "Фото этого акта подойдёт?", "Да, фотографии принимаются. Документ можно отправить через приложение или по электронной почте, указав номер обращения.", "SC18", "Вопрос о формате недостающего документа, а не о статусе обращения.", [document]),
  ] };
}
