/**
 * jevcomp — Claude Code function-hook plugin (agent-universal Jev compaction).
 *
 * Implements the algorithm of fast-jev-compaction (MIT) as used by the jevcomp
 * Python core: no lossy summary — Jev answers calibrated yes/no (`noul`)
 * questions about each tool call; stale calls/results are deleted or
 * truncated, everything kept stays verbatim.
 *
 * Runs in the function-hooks sandbox: no Node, web APIs only, HTTP through
 * `$.http.fetch`. The same algorithm powers the Python CLI (`jevcomp`) used
 * by zcode / Codex / other agents.
 */

const HOOK_DEFAULTS = {
  compactAtPercent: 60,
  minReductionRatio: 0.25,
  model: 'jev-latest',
};

const STATE_CONTEXT =
  'A coding assistant conversation is being compacted to free context. `history` is the whole conversation so far, oldest first; tool outputs are replaced by a short `result` note and long texts may be abridged. Each question asks whether one tool call, or the full output of that call, still needs to stay in the history verbatim. Whatever is not kept is deleted permanently, but the assistant can always re-run a tool or re-read a file.';

const INPUT_CHARS = [1000, 200, 60];
const TEXT_HEAD = 400;
const TEXT_TAIL = 150;
const REQUEST_OVERHEAD_TOKENS = 20;

// ---------------------------------------------------------------- tokens

const TOKEN_PIECES = /[A-Za-z]+|\d+|[^\sA-Za-z\d]/g;

function estimateTokens(text) {
  let tokens = 0;
  for (const piece of text.match(TOKEN_PIECES) || []) {
    const first = piece.charCodeAt(0);
    if (first >= 48 && first <= 57) tokens += piece.length / 2;
    else if ((first >= 65 && first <= 90) || (first >= 97 && first <= 122))
      tokens += 1 + Math.floor((piece.length - 1) / 6);
    else tokens += 0.9;
  }
  return Math.ceil(tokens);
}

function truncate(text, limit) {
  return text.length <= limit ? text : text.slice(0, Math.max(0, limit - 1)) + '…';
}

function abridge(text, head, tail) {
  if (text.length <= head + tail + 40) return text;
  const omitted = text.length - head - tail;
  return `${text.slice(0, head)}\n[… ${omitted} chars omitted …]\n${text.slice(-tail)}`;
}

// ---------------------------------------------------------------- collect

function isPinned(index, total, preserveRecentMessages) {
  return index === 0 || index >= total - preserveRecentMessages;
}

function collectToolCalls(messages, preserveRecentMessages) {
  const results = new Map();
  messages.forEach((message, index) => {
    for (const result of message.toolResults || [])
      results.set(result.tool_use_id, { index, result });
  });
  const calls = [];
  messages.forEach((message, callIndex) => {
    for (const tool of message.toolUses || []) {
      const found = results.get(tool.tool_use_id);
      if (!found) continue;
      calls.push({
        id: `t${calls.length + 1}`,
        tool_use_id: tool.tool_use_id,
        tool: tool.tool,
        input: tool.input || {},
        callIndex,
        resultIndex: found.index,
        resultChars: (found.result.text || '').length,
        isError: !!(found.result.isError ?? tool.isError),
        pinned:
          isPinned(callIndex, messages.length, preserveRecentMessages) ||
          isPinned(found.index, messages.length, preserveRecentMessages),
      });
    }
  });
  return calls;
}

// ---------------------------------------------------------------- state

function inputText(input, limit) {
  let json;
  try {
    json = JSON.stringify(input);
  } catch {
    json = '[unserializable input]';
  }
  return truncate(json, limit);
}

function resultNote(call) {
  return `${call.isError ? 'error' : 'ok'}, ${call.resultChars} chars (omitted)`;
}

function compactCallLine(call) {
  const parts = Object.entries(call.input).map(([key, value]) => {
    const text = typeof value === 'string' ? value : inputText({ [key]: value }, 200);
    return `${key}=${text.replace(/\s+/g, ' ')}`;
  });
  return `${call.id} ${call.tool} ${truncate(parts.join(' '), INPUT_CHARS[2])} → ${
    call.isError ? 'error' : 'ok'
  } ${call.resultChars}ch`;
}

function historyEntries(messages, calls, inputChars) {
  const byMessage = new Map();
  for (const call of calls) {
    const list = byMessage.get(call.callIndex) || [];
    list.push(call);
    byMessage.set(call.callIndex, list);
  }
  const entries = [];
  messages.forEach((message, i) => {
    const toolCalls = (byMessage.get(i) || []).map((call) => ({
      id: call.id,
      tool: call.tool,
      input: inputText(call.input, inputChars),
      result: resultNote(call),
    }));
    if (!(message.text || '').trim() && toolCalls.length === 0) return;
    const entry = { i, role: message.role, text: message.text || '' };
    if (toolCalls.length > 0) entry.tool_calls = toolCalls;
    entries.push(entry);
  });
  return entries;
}

function goalFromMessages(messages) {
  return messages
    .filter(
      (message) =>
        message.role === 'user' &&
        (message.text || '').trim().length > 0 &&
        (message.toolResults || []).length === 0,
    )
    .slice(-3)
    .map((message) => truncate(message.text, 500))
    .join('\n');
}

function mergeCallRuns(history, pinned) {
  const merged = [];
  const foldable = (e) =>
    !pinned(e) && e.text.length === 0 && Array.isArray(e.tool_calls) && typeof e.tool_calls[0] === 'string';
  for (const entry of history) {
    const previous = merged[merged.length - 1];
    if (previous && foldable(previous) && foldable(entry) && previous.role === entry.role) {
      previous.tool_calls = [...previous.tool_calls, ...entry.tool_calls];
      continue;
    }
    merged.push({ ...entry, tool_calls: entry.tool_calls ? [...entry.tool_calls] : undefined });
  }
  return merged;
}

function fitState(messages, calls, options) {
  const goal = options.goal || goalFromMessages(messages);
  const stateOf = (history) => ({ context: STATE_CONTEXT, goal, history });
  const entryTokens = (entry) => estimateTokens(JSON.stringify(entry)) + 1;
  const baseTokens = estimateTokens(JSON.stringify(stateOf([])));
  const maxTokens = options.maxStateTokens;

  let history = [];
  let perEntry = [];
  let tokens = 0;
  const rebuild = (inputChars) => {
    history = historyEntries(messages, calls, inputChars);
    perEntry = history.map(entryTokens);
    tokens = baseTokens + perEntry.reduce((sum, n) => sum + n, 0);
  };
  const fits = () => tokens <= maxTokens;
  const fitted = (stage) => ({ state: stateOf(history), tokens, stage });
  const shrink = (index, change) => {
    const entry = history[index];
    if (!entry) return;
    change(entry);
    const now = entryTokens(entry);
    tokens += now - (perEntry[index] || 0);
    perEntry[index] = now;
  };

  rebuild(INPUT_CHARS[0]);
  if (fits()) return fitted('full');
  for (const limit of INPUT_CHARS.slice(1)) {
    rebuild(limit);
    if (fits()) return fitted(`inputs<=${limit}`);
  }

  const pinned = (entry) => isPinned(entry.i, messages.length, options.preserveRecentMessages);
  const order = [
    ...history.map((_, index) => index).filter((index) => !pinned(history[index])),
    ...history.map((_, index) => index).filter((index) => pinned(history[index])),
  ];

  for (const index of order) {
    const entry = history[index];
    if (entry.text.length <= TEXT_HEAD + TEXT_TAIL + 40) continue;
    shrink(index, (e) => {
      e.text = abridge(e.text, TEXT_HEAD, TEXT_TAIL);
    });
    if (fits()) return fitted('texts abridged');
  }
  for (const index of order) {
    const entry = history[index];
    if (pinned(entry) || entry.text.length === 0) continue;
    const original = (messages[entry.i] && messages[entry.i].text || '').length;
    shrink(index, (e) => {
      e.text = `[… ${original} chars omitted …]`;
    });
    if (fits()) return fitted('old messages collapsed');
  }
  const byMessage = new Map();
  for (const call of calls) {
    const list = byMessage.get(call.callIndex) || [];
    list.push(call);
    byMessage.set(call.callIndex, list);
  }
  for (const index of order) {
    const entry = history[index];
    const own = byMessage.get(entry.i);
    if (pinned(entry) || !own) continue;
    shrink(index, (e) => {
      e.tool_calls = own.map(compactCallLine);
    });
    if (fits()) return fitted('old calls compacted');
  }
  const left = new Set();
  for (const index of order) {
    const entry = history[index];
    if (pinned(entry) || entry.tool_calls) continue;
    left.add(index);
    tokens -= perEntry[index] || 0;
    if (fits()) {
      history = history.filter((_, i) => !left.has(i));
      return fitted('old messages left out');
    }
  }
  history = mergeCallRuns(
    history.filter((_, i) => !left.has(i)),
    pinned,
  );
  perEntry = history.map(entryTokens);
  tokens = baseTokens + perEntry.reduce((sum, n) => sum + n, 0);
  if (fits()) return fitted('old calls merged');
  throw new Error(
    `history too large for Jev (~${tokens} tokens after truncation, limit ${maxTokens})`,
  );
}

// ---------------------------------------------------------------- ask

function questionsFor(call) {
  return {
    [`call_${call.id}`]: {
      type: 'noul',
      instructions: `Tool call ${call.id} (${call.tool}) should stay in the history: knowing this call was made, with its input, still matters for what the assistant does next`,
    },
    [`result_${call.id}`]: {
      type: 'noul',
      instructions: `The full output of tool call ${call.id} (${call.tool}, ${call.resultChars} chars) should stay in the history verbatim: the assistant still needs its contents and re-running the tool would not do`,
    },
  };
}

function batchCalls(calls, stateTokens, options) {
  const budget = options.maxRequestTokens - stateTokens - REQUEST_OVERHEAD_TOKENS;
  const batches = [];
  let current = [];
  let currentTokens = 0;
  for (const call of calls) {
    const tokens = estimateTokens(JSON.stringify(questionsFor(call)));
    if (current.length > 0 && currentTokens + tokens > budget) {
      batches.push(current);
      current = [];
      currentTokens = 0;
    }
    if (current.length === 0 && tokens > budget) {
      throw new Error(
        `state leaves no room for questions (~${stateTokens} of ${options.maxRequestTokens} tokens)`,
      );
    }
    current.push(call);
    currentTokens += tokens;
  }
  if (current.length > 0) batches.push(current);
  return batches;
}

function noulAnswer(answers, name) {
  const answer = answers[name];
  if (!answer || typeof answer.noul !== 'number' || !Number.isFinite(answer.noul)) {
    throw new Error(`Invalid Jev answer for ${name}`);
  }
  return answer.noul;
}

async function askBatch(fetchFn, apiKey, model, baseUrl, state, batch) {
  const questions = Object.assign({}, ...batch.map(questionsFor));
  const response = await fetchFn(baseUrl || 'https://api.typesafe.ai/v1/systemone', {
    method: 'POST',
    headers: {
      authorization: `Bearer ${apiKey}`,
      'content-type': 'application/json',
    },
    body: JSON.stringify({ model: model || 'jev-latest', state, questions }),
  });
  if (!response.ok) {
    throw new Error(`Jev request failed (${response.status}): ${(response.text || '').slice(0, 200)}`);
  }
  let parsed;
  try {
    parsed = JSON.parse(response.text);
  } catch {
    throw new Error('Jev returned malformed JSON');
  }
  if (!parsed || typeof parsed !== 'object' || !parsed.answers) {
    throw new Error('Jev response is missing answers');
  }
  const answers = {};
  for (const call of batch) {
    answers[call.id] = {
      keepCall: noulAnswer(parsed.answers, `call_${call.id}`),
      keepResult: noulAnswer(parsed.answers, `result_${call.id}`),
    };
  }
  return answers;
}

// ---------------------------------------------------------------- decide

function decideCall(call, answer, options) {
  if (call.pinned) return { action: 'keep', reason: 'pinned' };
  if (answer.keepResult >= options.keepThreshold) return { action: 'keep', reason: 'kept' };
  if (answer.keepCall >= options.keepThreshold)
    return { action: 'drop_result', reason: 'result_dropped' };
  return { action: 'drop_call', reason: 'call_dropped' };
}

function truncatedResultText(text, isError, headChars) {
  if (text.length <= headChars + 120) return text;
  const head = headChars > 0 ? `${text.slice(0, headChars)}\n` : '';
  return `${head}[jevcomp truncated ${text.length - headChars} chars of this tool result${
    isError ? ' (error)' : ''
  }; re-run the tool if needed]`;
}

function applyDecisions(messages, decisions, calls, headChars) {
  const byId = new Map(calls.map((call) => [call.id, call]));
  const actions = new Map();
  for (const decision of decisions) {
    const call = byId.get(decision.id);
    if (call && decision.action !== 'keep') actions.set(call.tool_use_id, decision.action);
  }
  const kept = [];
  for (const message of messages) {
    const toolUses = message.toolUses || [];
    const toolResults = message.toolResults || [];
    const touched =
      toolUses.some((tool) => actions.has(tool.tool_use_id)) ||
      toolResults.some((result) => actions.has(result.tool_use_id));
    if (!touched) {
      kept.push(message);
      continue;
    }
    const newUses = toolUses
      .filter((tool) => actions.get(tool.tool_use_id) !== 'drop_call')
      .map((tool) => {
        if (actions.get(tool.tool_use_id) !== 'drop_result') return tool;
        const text = truncatedResultText(tool.text || '', !!tool.isError, headChars);
        return { ...tool, text };
      });
    const newResults = toolResults
      .filter((result) => actions.get(result.tool_use_id) !== 'drop_call')
      .map((result) => {
        if (actions.get(result.tool_use_id) !== 'drop_result') return result;
        return {
          ...result,
          text: truncatedResultText(result.text || '', !!result.isError, headChars),
        };
      });
    if (!(message.text || '').trim() && newUses.length === 0 && newResults.length === 0) continue;
    const rebuilt = { role: message.role, text: message.text, toolUses: newUses };
    if (newResults.length > 0) rebuilt.toolResults = newResults;
    kept.push(rebuilt);
  }
  return kept;
}

function messageChars(message) {
  let total = (message.text || '').length;
  for (const tool of message.toolUses || []) {
    try {
      total += JSON.stringify(tool.input || {}).length;
    } catch {
      total += 20;
    }
  }
  for (const result of message.toolResults || []) total += (result.text || '').length;
  return total;
}

// ---------------------------------------------------------------- compact

async function compact(fetchFn, apiKey, model, baseUrl, messages, options) {
  const started = Date.now();
  const calls = collectToolCalls(messages, options.preserveRecentMessages);
  const candidates = calls.filter((call) => !call.pinned);
  const charsBefore = messages.reduce((sum, message) => sum + messageChars(message), 0);

  let fitted = { tokens: 0, stage: '' };
  let batches = [];
  const answers = {};
  if (candidates.length > 0) {
    fitted = fitState(messages, calls, options);
    batches = batchCalls(candidates, fitted.tokens, options);
    const parts = await Promise.all(
      batches.map((batch) => askBatch(fetchFn, apiKey, model, baseUrl, fitted.state, batch)),
    );
    for (const part of parts) Object.assign(answers, part);
  }

  const decisions = calls.map((call) => {
    const answer = answers[call.id] || { keepCall: 1, keepResult: 1 };
    return { id: call.id, tool: call.tool, ...answer, ...decideCall(call, answer, options) };
  });
  const kept = applyDecisions(messages, decisions, calls, options.truncateHeadChars);
  const count = (reason) => decisions.filter((decision) => decision.reason === reason).length;
  const charsAfter = kept.reduce((sum, message) => sum + messageChars(message), 0);
  return {
    messages: kept,
    decisions,
    stats: {
      messagesBefore: messages.length,
      messagesAfter: kept.length,
      charsBefore,
      charsAfter,
      calls: calls.length,
      kept: count('kept'),
      resultsDropped: count('result_dropped'),
      callsDropped: count('call_dropped'),
      pinned: count('pinned'),
      stateTokens: fitted.tokens,
      stateStage: fitted.stage,
      requests: batches.length,
      ms: Date.now() - started,
    },
  };
}

function reductionRatio(stats) {
  return stats.charsBefore === 0 ? 0 : (stats.charsBefore - stats.charsAfter) / stats.charsBefore;
}

// ---------------------------------------------------------------- hook glue

function optionNumber(options, key, fallback) {
  const value = options[key];
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}

function optionString(options, key) {
  const value = options[key];
  return typeof value === 'string' && value.length > 0 ? value : undefined;
}

function envNumber(name, fallback) {
  try {
    const value = Number(globalThis?.process?.env?.[name] ?? globalThis?.[name]);
    return Number.isFinite(value) && value > 0 ? value : fallback;
  } catch {
    return fallback;
  }
}

function resolveHookConfig(options) {
  return {
    keepThreshold: optionNumber(options, 'keepThreshold', 0.5),
    preserveRecentMessages: optionNumber(options, 'preserveRecentMessages', 6),
    maxStateTokens: optionNumber(options, 'maxStateTokens', 25000),
    maxRequestTokens: optionNumber(options, 'maxRequestTokens', 30000),
    truncateHeadChars: optionNumber(options, 'truncateHeadChars', 300),
    compactAtPercent: optionNumber(
      options,
      'compactAtPercent',
      envNumber('JEVCOMP_COMPACT_AT_PERCENT', HOOK_DEFAULTS.compactAtPercent),
    ),
    minReductionRatio: optionNumber(options, 'minReductionRatio', HOOK_DEFAULTS.minReductionRatio),
    model: optionString(options, 'model') || HOOK_DEFAULTS.model,
    apiKey: optionString(options, 'apiKey'),
    goal: optionString(options, 'goal'),
    baseUrl: optionString(options, 'baseUrl'),
  };
}

async function getApiKey($, config) {
  if (config.apiKey) return config.apiKey;
  const fromEnv = await $.env.get('TYPESAFE_API_KEY');
  if (fromEnv) return fromEnv;
  const settings = await $.settings.read();
  const env = settings && settings['env'];
  if (env && typeof env === 'object') {
    const value = env['TYPESAFE_API_KEY'];
    if (typeof value === 'string' && value) return value;
  }
  return undefined;
}

function summarize(stats) {
  const parts = [
    stats.kept > 0 ? `${stats.kept} kept` : '',
    stats.resultsDropped > 0 ? `${stats.resultsDropped} results truncated` : '',
    stats.callsDropped > 0 ? `${stats.callsDropped} call_dropped` : '',
    stats.pinned > 0 ? `${stats.pinned} pinned` : '',
  ].filter(Boolean);
  return `${Math.round(reductionRatio(stats) * 100)}% reduction; ${
    parts.join(', ') || 'no tool calls'
  }; state ~${stats.stateTokens} tokens (${stats.stateStage}) in ${stats.requests} request(s)`;
}

function decisionLog(result, maxChars = 4096) {
  const entries = result.decisions
    .filter((d) => d.reason !== 'pinned')
    .map(
      (d) =>
        `${d.id}:${d.tool}:${d.action}/call=${d.keepCall.toFixed(2)}/result=${d.keepResult.toFixed(2)}`,
    );
  const chunks = [];
  let current = '';
  for (const entry of entries) {
    const next = current ? `${current} ${entry}` : entry;
    if (current && next.length > maxChars - 24) {
      chunks.push(current);
      current = entry;
    } else current = next;
  }
  if (current) chunks.push(current);
  return chunks.length === 0
    ? ['decisions: (none)']
    : chunks.map((chunk, index) =>
        chunks.length === 1 ? `decisions: ${chunk}` : `decisions (${index + 1}/${chunks.length}): ${chunk}`,
      );
}

function notify($, text) {
  try {
    $.ui.log(text);
    $.ui.toast(text, { timeoutMs: 15000 });
  } catch {
    /* headless: toasts may not exist */
  }
}

/** @type {import('claude-code').Register} */
export const register = (on, options) => {
  const configured = resolveHookConfig(options);
  let compacting = false;

  on('session.compact', async ($, event, next) => {
    try {
      const apiKey = await getApiKey($, configured);
      if (!apiKey) throw new Error('TYPESAFE_API_KEY is not configured');
      const fetchFn = async (url, init) => {
        const response = await $.http.fetch(url, init);
        return {
          status: response.status,
          ok: response.ok,
          text: typeof response.text === 'string' ? response.text : await response.text(),
        };
      };
      const result = await compact(
        fetchFn,
        apiKey,
        configured.model,
        configured.baseUrl,
        event.messages,
        configured,
      );
      for (const line of decisionLog(result)) $.ui.log(line);
      if (reductionRatio(result.stats) < configured.minReductionRatio) {
        notify(
          $,
          `fallback to built-in summary (below ${Math.round(
            configured.minReductionRatio * 100,
          )}% minimum: ${summarize(result.stats)})`,
        );
        return next(event);
      }
      notify(
        $,
        `jevcomp: kept ${result.stats.messagesAfter}/${event.messages.length} messages, no summary (${summarize(
          result.stats,
        )})`,
      );
      return { messages: result.messages };
    } catch (error) {
      notify(
        $,
        `fallback to built-in summary (${error instanceof Error ? error.message : String(error)})`,
      );
      return next(event);
    }
  });

  on('turn.complete', async ($, event, next) => {
    if (compacting) return next(event);
    try {
      const { context } = await $.session.usage();
      if ((context?.percent ?? 0) < configured.compactAtPercent) return next(event);
      compacting = true;
      await $.session.compact();
    } catch (error) {
      $.ui.log(
        `auto-compact skipped (${error instanceof Error ? error.message : String(error)})`,
      );
    } finally {
      compacting = false;
    }
    return next(event);
  });
};
