import type { AskAnswersStructured } from "@/modules/chat/components/AskCard";

export function askAnswersStructuredRequestFields(
  answers: unknown,
): Record<string, unknown> {
  if (!answers || typeof answers !== "object") {
    return {};
  }
  return { ask_answers_structured: answers };
}

export function withAskAnswersStructured(
  extras: Record<string, unknown>,
  answers?: AskAnswersStructured,
): Record<string, unknown> {
  if (!answers) {
    return extras;
  }
  return { ...extras, ...askAnswersStructuredRequestFields(answers) };
}
