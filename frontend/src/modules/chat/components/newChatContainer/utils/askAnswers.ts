import type { AskAnswersStructured } from "@/modules/chat/components/AskCard";

export function withAskAnswersStructured(
  extras: Record<string, unknown>,
  answers?: AskAnswersStructured,
): Record<string, unknown> {
  if (!answers) {
    return extras;
  }
  return { ...extras, ask_answers_structured: answers };
}
