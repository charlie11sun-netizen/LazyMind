import { describe, expect, it } from "vitest";
import {
  askAnswersStructuredRequestFields,
  withAskAnswersStructured,
} from "./askAnswers";

describe("withAskAnswersStructured", () => {
  it("forwards an AskCard submission to the chat request extras", () => {
    const answers = {
      ask_id: "ask-1",
      questions: [
        {
          text: "Which structure?",
          type: "single" as const,
          choices: ["Continuous prose", "Sectioned"],
          custom_choices: ["Continuous prose", "Sectioned"],
          answer: { type: "single" as const, value: "Continuous prose", otherText: "" },
        },
      ],
    };

    expect(withAskAnswersStructured({ thinking_depth: "medium" }, answers)).toEqual({
      thinking_depth: "medium",
      ask_answers_structured: answers,
    });
  });

  it("leaves ordinary chat extras unchanged", () => {
    const extras = { thinking_depth: "low" };
    expect(withAskAnswersStructured(extras)).toBe(extras);
  });

  it("creates the final HTTP request fields from forwarded extras", () => {
    const answers = { ask_id: "ask-2", questions: [] };
    const extras = withAskAnswersStructured({}, answers);

    expect(
      askAnswersStructuredRequestFields(extras.ask_answers_structured),
    ).toEqual({ ask_answers_structured: answers });
    expect(askAnswersStructuredRequestFields(undefined)).toEqual({});
  });
});
