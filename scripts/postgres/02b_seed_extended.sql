-- 更全面的演示数据：缺少信息、外部系统降级、公开 FAQ 三类 Case。
-- 全部 INSERT 都是幂等的，不覆盖运行期产生的数据。
\set ON_ERROR_STOP on

-- =============================================================================
-- 一、缺少订单引用，需要旅客补充信息
-- =============================================================================

INSERT INTO conversations(
    conversation_id, verified_subject_id, locale, created_at, updated_at
) VALUES (
    'seed_conv_clarification', 'subject_demo', 'zh-CN',
    '2026-07-29T02:00:00+00:00', '2026-07-29T02:00:10+00:00'
) ON CONFLICT(conversation_id) DO NOTHING;

INSERT INTO messages(
    message_id, conversation_id, role, content, request_id, created_at
) VALUES (
    'seed_msg_clarification_001', 'seed_conv_clarification', 'user',
    '我的退款怎么还没到账？', 'seed_req_clarification',
    '2026-07-29T02:00:00+00:00'
) ON CONFLICT(message_id) DO NOTHING;

INSERT INTO cases(
    case_id, conversation_id, request_id, status, user_goal, case_summary,
    plan_json, created_at, updated_at
) VALUES (
    'seed_case_clarification', 'seed_conv_clarification',
    'seed_req_clarification', 'waiting_for_information',
    '查询退款到账状态', '缺少 PNR、票号、订单号或退款单号，尚未访问敏感记录。',
    '{"case_type":"clarification","user_goal":"查询退款到账状态","intents":["refund_status"],"missing_fields":["refund_reference"],"tasks":[],"parallel":false,"human_action_likely":false}',
    '2026-07-29T02:00:00+00:00', '2026-07-29T02:00:10+00:00'
) ON CONFLICT(case_id) DO NOTHING;

INSERT INTO service_responses(
    response_id, case_id, response_version, payload_json, created_at
) VALUES (
    'seed_resp_clarification_v1', 'seed_case_clarification', 1,
    '{"response_status":"needs_clarification","answer":"请提供 PNR、客票号、订单号或退款单号中的任意一项，我才能在身份验证后查询退款进度。","verified_facts":[],"available_options":[],"missing_information":["refund_reference"],"handoff_required":false,"handoff_reason":null,"must_not_claim":["退款状态","到账日期"]}',
    '2026-07-29T02:00:10+00:00'
) ON CONFLICT(case_id, response_version) DO NOTHING;

INSERT INTO trace_events(
    event_id, trace_id, case_id, event_type, sequence_no, payload_json, created_at
) VALUES
('seed_trace_clarification_001', 'seed_trace_clarification', 'seed_case_clarification',
 'request.received', 1, '{"requestId":"seed_req_clarification","conversationId":"seed_conv_clarification"}',
 '2026-07-29T02:00:00+00:00'),
('seed_trace_clarification_002', 'seed_trace_clarification', 'seed_case_clarification',
 'coordinator.clarification_required', 2, '{"missingFields":["refund_reference"],"toolCallCount":0}',
 '2026-07-29T02:00:05+00:00'),
('seed_trace_clarification_003', 'seed_trace_clarification', 'seed_case_clarification',
 'case.completed', 3, '{"status":"waiting_for_information","toolCallCount":0,"evidenceCount":0}',
 '2026-07-29T02:00:10+00:00')
ON CONFLICT(case_id, sequence_no) DO NOTHING;

-- =============================================================================
-- 二、退款系统超时，系统明确降级且不捏造状态
-- =============================================================================

INSERT INTO conversations(
    conversation_id, verified_subject_id, locale, created_at, updated_at
) VALUES (
    'seed_conv_degraded', 'subject_demo', 'zh-CN',
    '2026-07-29T02:30:00+00:00', '2026-07-29T02:30:30+00:00'
) ON CONFLICT(conversation_id) DO NOTHING;

INSERT INTO messages(
    message_id, conversation_id, role, content, request_id, created_at
) VALUES (
    'seed_msg_degraded_001', 'seed_conv_degraded', 'user',
    '请查询退款单 RF1001 的最新状态。', 'seed_req_degraded',
    '2026-07-29T02:30:00+00:00'
) ON CONFLICT(message_id) DO NOTHING;

INSERT INTO cases(
    case_id, conversation_id, request_id, status, user_goal, case_summary,
    plan_json, created_at, updated_at
) VALUES (
    'seed_case_degraded', 'seed_conv_degraded', 'seed_req_degraded', 'responded',
    '查询退款单 RF1001 最新状态',
    '退款系统连续两次超时，当前无法建立最新退款事实，已按降级回复。',
    '{"case_type":"refund","user_goal":"查询退款单 RF1001 最新状态","intents":["refund_status"],"missing_fields":[],"tasks":[{"task_id":"seed_task_degraded","domain":"refund","objective":"核实 RF1001 最新状态","entity_refs":{"refund_ref":"RF1001"},"allowed_tools":["get_refund_status","search_airline_knowledge","get_policy_clause"],"required_evidence":["refund","policy"],"max_tool_calls":4}],"parallel":false,"human_action_likely":false}',
    '2026-07-29T02:30:00+00:00', '2026-07-29T02:30:30+00:00'
) ON CONFLICT(case_id) DO NOTHING;

INSERT INTO tool_calls(
    tool_call_id, case_id, invocation_id, task_id, domain, tool_name,
    arguments_json, status, error_code, started_at, ended_at
) VALUES (
    'seed_tc_degraded_refund', 'seed_case_degraded', 'seed_inv_degraded',
    'seed_task_degraded', 'refund', 'get_refund_status',
    '{"refund_ref":"RF1001"}', 'timeout', 'UPSTREAM_TIMEOUT',
    '2026-07-29T02:30:10+00:00', '2026-07-29T02:30:20+00:00'
) ON CONFLICT(tool_call_id) DO NOTHING;

INSERT INTO service_responses(
    response_id, case_id, response_version, payload_json, created_at
) VALUES (
    'seed_resp_degraded_v1', 'seed_case_degraded', 1,
    '{"response_status":"degraded","answer":"退款系统当前超时，我无法核实 RF1001 的最新处理阶段。请稍后重试；当前结论不能用于确认退款完成或到账。","verified_facts":[],"available_options":[{"option":"稍后重新查询退款状态","execution_status":"not_executed","evidence_ids":[]}],"missing_information":["latest_refund_status"],"handoff_required":false,"handoff_reason":null,"must_not_claim":["退款已完成","退款已经到账"]}',
    '2026-07-29T02:30:30+00:00'
) ON CONFLICT(case_id, response_version) DO NOTHING;

INSERT INTO trace_events(
    event_id, trace_id, case_id, event_type, sequence_no, payload_json, created_at
) VALUES
('seed_trace_degraded_001', 'seed_trace_degraded', 'seed_case_degraded',
 'request.received', 1, '{"requestId":"seed_req_degraded"}',
 '2026-07-29T02:30:00+00:00'),
('seed_trace_degraded_002', 'seed_trace_degraded', 'seed_case_degraded',
 'tool.completed', 2, '{"toolCallId":"seed_tc_degraded_refund","toolName":"get_refund_status","status":"timeout","attempt":2,"evidenceIds":[]}',
 '2026-07-29T02:30:20+00:00'),
('seed_trace_degraded_003', 'seed_trace_degraded', 'seed_case_degraded',
 'quality.checked', 3, '{"decision":"pass","violations":[],"degraded":true}',
 '2026-07-29T02:30:29+00:00'),
('seed_trace_degraded_004', 'seed_trace_degraded', 'seed_case_degraded',
 'case.completed', 4, '{"status":"responded","toolCallCount":1,"evidenceCount":0}',
 '2026-07-29T02:30:30+00:00')
ON CONFLICT(case_id, sequence_no) DO NOTHING;

-- =============================================================================
-- 三、公开航班查询：展示无需旅客身份即可调用 public_read Tool
-- =============================================================================

INSERT INTO conversations(
    conversation_id, verified_subject_id, locale, created_at, updated_at
) VALUES (
    'seed_conv_public_flight', NULL, 'zh-CN',
    '2026-07-29T03:00:00+00:00', '2026-07-29T03:00:20+00:00'
) ON CONFLICT(conversation_id) DO NOTHING;

INSERT INTO messages(
    message_id, conversation_id, role, content, request_id, created_at
) VALUES (
    'seed_msg_public_flight_001', 'seed_conv_public_flight', 'user',
    'CZ8888 航班 2026-07-29 正常吗？', 'seed_req_public_flight',
    '2026-07-29T03:00:00+00:00'
) ON CONFLICT(message_id) DO NOTHING;

INSERT INTO cases(
    case_id, conversation_id, request_id, status, user_goal, case_summary,
    plan_json, created_at, updated_at
) VALUES (
    'seed_case_public_flight', 'seed_conv_public_flight',
    'seed_req_public_flight', 'responded', '查询公开航班状态',
    'Fixture 航班运行系统返回 CZ8888 当前为 ON_TIME。',
    '{"case_type":"journey","user_goal":"查询公开航班状态","intents":["flight_status"],"missing_fields":[],"tasks":[{"task_id":"seed_task_public_flight","domain":"journey","objective":"核实 CZ8888 航班状态","entity_refs":{"flight_no":"CZ8888","travel_date":"2026-07-29"},"allowed_tools":["get_flight_status"],"required_evidence":["flight"],"max_tool_calls":1}],"parallel":false,"human_action_likely":false}',
    '2026-07-29T03:00:00+00:00', '2026-07-29T03:00:20+00:00'
) ON CONFLICT(case_id) DO NOTHING;

INSERT INTO tool_calls(
    tool_call_id, case_id, invocation_id, task_id, domain, tool_name,
    arguments_json, status, error_code, started_at, ended_at
) VALUES (
    'seed_tc_public_flight', 'seed_case_public_flight', 'seed_inv_public_flight',
    'seed_task_public_flight', 'journey', 'get_flight_status',
    '{"flight_no":"CZ8888","date":"2026-07-29"}', 'success', NULL,
    '2026-07-29T03:00:10+00:00', '2026-07-29T03:00:10.025+00:00'
) ON CONFLICT(tool_call_id) DO NOTHING;

INSERT INTO evidence_items(
    evidence_id, case_id, source_type, source_id, authority, version,
    payload_json, created_at
) VALUES (
    'seed_ev_public_flight', 'seed_case_public_flight',
    'fixture_get_flight_status', 'CZ8888:2026-07-29',
    'system_of_record', 'airline-mvp-v1',
    '{"evidence_id":"seed_ev_public_flight","case_id":"seed_case_public_flight","evidence_type":"flight","source_type":"fixture_get_flight_status","source_id":"CZ8888:2026-07-29","authority":"system_of_record","summary":"CZ8888 当前状态为 ON_TIME","structured_data":{"status":"ON_TIME"},"observed_at":"2026-07-29T03:00:10+00:00","valid_from":null,"valid_to":null,"version":"airline-mvp-v1","locator":{"fixture":"flights.json","flightNo":"CZ8888"},"confidence":1.0}',
    '2026-07-29T03:00:10+00:00'
) ON CONFLICT(evidence_id) DO NOTHING;

INSERT INTO service_responses(
    response_id, case_id, response_version, payload_json, created_at
) VALUES (
    'seed_resp_public_flight_v1', 'seed_case_public_flight', 1,
    '{"response_status":"answered","answer":"已核实：CZ8888 在 2026 年 7 月 29 日当前状态为正常。航班运行状态可能变化，请以临近起飞时的最新查询为准。","verified_facts":[{"statement":"CZ8888 当前状态为 ON_TIME","evidence_ids":["seed_ev_public_flight"],"confidence":1.0,"uncertainty":"运行状态可能变化"}],"available_options":[],"missing_information":[],"handoff_required":false,"handoff_reason":null,"must_not_claim":[]}',
    '2026-07-29T03:00:20+00:00'
) ON CONFLICT(case_id, response_version) DO NOTHING;

INSERT INTO trace_events(
    event_id, trace_id, case_id, event_type, sequence_no, payload_json, created_at
) VALUES
('seed_trace_public_001', 'seed_trace_public', 'seed_case_public_flight',
 'request.received', 1, '{"requestId":"seed_req_public_flight"}',
 '2026-07-29T03:00:00+00:00'),
('seed_trace_public_002', 'seed_trace_public', 'seed_case_public_flight',
 'tool.completed', 2, '{"toolCallId":"seed_tc_public_flight","toolName":"get_flight_status","status":"success","evidenceIds":["seed_ev_public_flight"]}',
 '2026-07-29T03:00:10+00:00'),
('seed_trace_public_003', 'seed_trace_public', 'seed_case_public_flight',
 'case.completed', 3, '{"status":"responded","toolCallCount":1,"evidenceCount":1}',
 '2026-07-29T03:00:20+00:00')
ON CONFLICT(case_id, sequence_no) DO NOTHING;
