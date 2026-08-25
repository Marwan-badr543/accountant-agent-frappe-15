# -*- coding: utf-8 -*-
# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see license.txt

"""What a question looks like once it is sitting in somebody's chat history.

WHAT WAS WRONG

    A paused run answers with a JSON envelope, because the client needs the
    questions and their options as data. That envelope was being SAVED to the
    transcript and published as the assistant's message, so an accountant
    asking about a laptop sale got a chat bubble opening
    `{"type": "clarification", "questions": [{"id": ...` — with the question
    inside it twice. Their own reply was not stored at all, so the transcript
    showed a machine-readable question followed by nothing.

WHAT THESE TESTS PIN

    * The question is stored. The options are NOT: the choice has been made,
      the answer is the customer's own next message, and a list of the roads
      not taken between the two only makes the exchange harder to read.
    * EVERY stored question folds, with the question itself as the handle:
      *"it should be collabsable so user can oben or close to save chat window
      space in ui"*. What folds away is the preamble and the guidance around
      it, never the question — that is read live, above an open answer picker,
      and a clipped question there would be worse than no fold at all.
    * The customer's answer comes out of the envelope the picker sends, and
      the question it echoes back does not.
    * The structured questions still ride along invisibly, so reloading the
      page on a run that is still paused reopens the answer picker.
    * The carrier never starts a paragraph of its own. It is `display: none`;
      a `<p>` wrapped around it is not, and an empty paragraph under every
      question is exactly the wasted space this work removed.
    * Arabic survives the round trip, which `atob` alone would not.

No site is needed: every function under test is pure.

RUN THEM FROM THE BENCH ROOT, not from the app directory:

    cd ~/frappe/my-bench && ./env/bin/python -m unittest \\
        accountant_agent.agent_api.tests.test_stored_questions

`frappe._()` builds the multi-question summary, and it opens `logs/frappe.log`
relative to the current directory.
"""

import json
import unittest
from base64 import b64decode

from accountant_agent.accountant_agent.page.agent_chat.agent_chat import (
	_answer_text,
	_collapsible_question,
	_prose_only,
	_readable_response,
)


ONE = [{
	"id": "party",
	"question": "من هو العميل في هذه العملية؟",
	"options": ["سجلها كبيع نقدي", "كانت بالآجل"],
	"allow_custom": True,
}]

TWO = ONE + [{"id": "amount", "question": "How much was it?", "options": []}]

#: The card as the agent renders it: a preamble, the question, and a closing
#: line telling them they may type instead of tapping. Only the middle line is
#: worth a customer's eye once the exchange is over.
CARD = (
	"Before I record this, let me check one thing with you.\n\n"
	"**من هو العميل في هذه العملية؟**\n\n"
	"Tell me the name as it appears in your system, or pick from the list below."
)


def _payload(stored: str) -> list:
	"""The structured questions back out of the block, the way the browser
	reads them: find the attribute, un-base64 it, decode it as UTF-8."""
	packed = stored.split('data-questions="', 1)[1].split('"', 1)[0]
	return json.loads(b64decode(packed).decode("utf-8"))


class StoredQuestionTests(unittest.TestCase):
	def test_the_options_are_not_stored_in_the_transcript(self):
		stored = _collapsible_question(ONE[0]["question"], ONE)
		self.assertIn("من هو العميل في هذه العملية؟", stored)
		for option in ONE[0]["options"]:
			self.assertNotIn(option, stored)

	def test_a_lone_question_is_stored_as_the_question_and_nothing_else(self):
		"""*"i said before the question should saved with just user answer, but
		you saved the question with all option so next nodes can not know whawt
		the user say here, so its the third time i tell you that"*.

		The first three attempts folded the RENDERED CARD, so the preamble and
		the closing guidance were stored under every question and handed back to
		the model on the next turn. Nothing is built from the card now.
		"""
		stored = _collapsible_question(CARD, ONE)
		self.assertIn("من هو العميل في هذه العملية؟", stored)
		self.assertNotIn("Before I record this", stored)
		self.assertNotIn("pick from the list below", stored)

	def test_a_lone_question_is_not_folded_because_there_is_nothing_to_fold(self):
		"""One line is already one line, and a fold that opens onto an empty box
		is a control that does nothing."""
		stored = _collapsible_question(CARD, ONE)
		self.assertNotIn("<details", stored)
		self.assertNotIn("<summary>", stored)

	def test_the_question_is_never_clipped(self):
		"""The same string is published LIVE, the moment the agent pauses. A
		truncated question above an open answer picker is worse than no fold."""
		stored = _collapsible_question(CARD, ONE)
		self.assertNotIn("...", stored)

	def test_several_questions_fold_with_the_first_as_the_handle(self):
		"""The fold earns its place only when there is something behind it:
		*"it should be collabsable so user can oben or close to save chat window
		space in ui"*."""
		stored = _collapsible_question(CARD, TWO)
		self.assertIn("<details", stored)
		# Closed by default: that is what saves the space.
		self.assertNotIn("<details open", stored)
		summary = stored.split("<summary>", 1)[1].split("</summary>", 1)[0]
		self.assertIn("من هو العميل في هذه العملية؟", summary)
		body = stored.split("</summary>", 1)[1]
		self.assertIn("How much was it?", body)
		# The handle is not repeated inside the fold, which is what anybody
		# opening it would otherwise read twice.
		self.assertNotIn("من هو العميل في هذه العملية؟", body)
		# And none of the card's scaffolding came with it.
		self.assertNotIn("Before I record this", stored)

	def test_a_reload_can_still_offer_the_answers(self):
		"""Whether it folded or not. The picker finds its questions by the
		`data-questions` attribute, and a lone question carries it on the
		invisible span instead of on the fold."""
		self.assertEqual(_payload(_collapsible_question(CARD, ONE)), ONE)
		self.assertEqual(_payload(_collapsible_question(CARD, TWO)), TWO)

	def test_several_questions_fold_but_still_show_none_of_the_options(self):
		stored = _collapsible_question(TWO[0]["question"], TWO)
		self.assertIn("<details", stored)
		self.assertIn("(and 1 more)", stored)
		# The second question is what folds away. The first is the summary.
		self.assertIn("How much was it?", stored)
		for option in ONE[0]["options"]:
			self.assertNotIn(option, stored)
		self.assertEqual(_payload(stored), TWO)

	def test_the_carrier_never_starts_a_paragraph_of_its_own(self):
		"""It is `display: none`; a `<p>` wrapped around it is not, and an
		empty paragraph under every question is exactly the wasted space this
		work set out to remove. The blank line before it is what stops
		Markdown giving the span its own block."""
		stored = _collapsible_question(CARD, ONE)
		self.assertTrue(stored.startswith(ONE[0]["question"]))
		self.assertIn("\n\n<span", stored)

	def test_a_question_with_no_wording_is_left_exactly_as_it_came(self):
		self.assertEqual(_collapsible_question("just this", []), "just this")
		self.assertEqual(
			_collapsible_question("just this", [{"id": "x", "question": ""}]),
			"just this",
		)


class AnswerTests(unittest.TestCase):
	def test_the_answer_is_stored_and_the_echoed_question_is_not(self):
		said = _answer_text(
			"Clarification Response:\n"
			"* **من هو العميل في هذه العملية؟**: سجلها كبيع نقدي"
		)
		self.assertEqual(said, "سجلها كبيع نقدي")

	def test_several_answers_are_stored_one_per_line(self):
		said = _answer_text(
			"Clarification Response:\n"
			"* **Who was it with?**: Delta Supplies\n"
			"* **How much?**: 500"
		)
		self.assertEqual(said, "Delta Supplies\n500")

	def test_nothing_to_show_stores_nothing(self):
		self.assertEqual(_answer_text("just a message"), "")
		self.assertEqual(_answer_text(""), "")


class EnvelopeTests(unittest.TestCase):
	def test_a_clarification_envelope_is_read_into_prose_and_questions(self):
		spoken, questions = _readable_response(json.dumps({
			"type": "clarification",
			"question": "من هو العميل في هذه العملية؟",
			"questions": ONE,
		}))
		self.assertEqual(spoken, "من هو العميل في هذه العملية؟")
		self.assertEqual(questions, ONE)

	def test_a_plan_envelope_is_left_byte_for_byte_alone(self):
		"""The approval gate finds a pending plan by matching the literal
		prefix `{"type": "plan"`. Prettifying it here would silently stop
		`Require Human Approval` working, from a function about wording."""
		plan = '{"type": "plan", "steps": []}'
		spoken, questions = _readable_response(plan)
		self.assertEqual(spoken, plan)
		self.assertEqual(questions, [])

	def test_an_ordinary_answer_is_not_touched(self):
		spoken, questions = _readable_response("I have recorded that for you.")
		self.assertEqual(spoken, "I have recorded that for you.")
		self.assertEqual(questions, [])


if __name__ == "__main__":
	unittest.main()


class HistoryToTheModelTests(unittest.TestCase):
	"""What the agent server is sent as the conversation so far.

	*"it should send to llm in chat history, so it can get the context and
	avoide ask the user the same question twice"*. It is sent the sentence the
	person actually read — never the envelope the client needs as data, and
	never the base64 the picker needs to survive a reload.
	"""

	def test_the_options_payload_is_stripped_out(self):
		stored = _collapsible_question(CARD, ONE)
		prose = _prose_only(stored)
		self.assertIn("من هو العميل في هذه العملية؟", prose)
		self.assertNotIn("data-questions", prose)
		self.assertNotIn("<details", prose)
		self.assertNotIn("<summary", prose)

	def test_a_plan_envelope_is_read_as_its_prose(self):
		"""Handing a model `{"type": "plan", ...}` teaches it to answer in
		JSON. What the customer read was the plan."""
		stored = json.dumps({
			"type": "plan", "plan": "Here is the entry I have prepared.",
			"status": "pending",
		})
		self.assertEqual(_prose_only(stored), "Here is the entry I have prepared.")

	def test_a_clarification_envelope_is_read_as_its_question(self):
		stored = json.dumps({
			"type": "clarification", "question": "Which supplier did you mean?",
			"questions": [{"id": "party", "options": ["Delta", "Acme"]}],
		})
		self.assertEqual(_prose_only(stored), "Which supplier did you mean?")

	def test_an_ordinary_turn_is_handed_over_untouched(self):
		self.assertEqual(
			_prose_only("i purchased paper supplies with 500 egp"),
			"i purchased paper supplies with 500 egp",
		)

	def test_an_empty_turn_yields_nothing_to_send(self):
		self.assertEqual(_prose_only("   "), "")
		self.assertEqual(_prose_only(None), "")

	def test_the_model_is_sent_the_question_and_none_of_the_scaffolding(self):
		"""THE BUG THIS PINS, FROM A REAL TRANSCRIPT.

		The agent asked for an amount, could not match the reply, and asked
		again — three times, of a customer who had said "submit those" and
		meant it. Each stored turn carried its apology and its standing
		guidance, so the history handed to the model read as an agent whose
		job is to ask for names. It obliged.
		"""
		card = (
			'Sorry — I could not match "don\'t recodr , they are draft, just '
			"submit them\" to a record in your system, so I do not want to "
			"guess at it.\n\n"
			"Before I record this, let me check one thing with you.\n\n"
			"**How much was it?**\n\n"
			"Tell me the name as it appears in your books and I will carry on."
		)
		prose = _prose_only(_collapsible_question(card, TWO))
		self.assertNotIn("Sorry", prose)
		self.assertNotIn("Before I record this", prose)
		self.assertNotIn("as it appears in your books", prose)
		# Both questions survive, because both were asked.
		self.assertIn("من هو العميل في هذه العملية؟", prose)
		self.assertIn("How much was it?", prose)

	def test_a_folded_question_reads_back_as_its_questions_not_its_summary(self):
		"""The summary of a multi-question fold says "(and 1 more)", which is
		chrome. The payload has both questions in full."""
		prose = _prose_only(_collapsible_question(CARD, TWO))
		self.assertNotIn("and 1 more", prose)
		self.assertEqual(prose.splitlines(), [q["question"] for q in TWO])

	def test_what_the_customer_typed_is_decoded_before_the_model_reads_it(self):
		"""The chat page stores a typed reply HTML-escaped, so `don't` is on
		disk as `don&#x27;t` and reached the model looking like markup."""
		self.assertEqual(
			_prose_only("don&#x27;t recodr , they are draft, just submit them"),
			"don't recodr , they are draft, just submit them",
		)

	def test_an_envelope_is_parsed_before_its_entities_are_decoded(self):
		"""The other order is a different bug: a `&quot;` inside a stored
		envelope becomes a real quote, closes the JSON string early, and the
		whole turn is handed over raw."""
		stored = json.dumps({
			"type": "plan", "plan": 'He said &quot;book it as cash&quot;.',
			"status": "pending",
		})
		self.assertEqual(_prose_only(stored), 'He said "book it as cash".')
