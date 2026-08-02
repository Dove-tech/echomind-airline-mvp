-- EchoMind Airline MVP：确定性演示数据
-- 固定主键统一使用 seed_ 前缀，所有写入均为 INSERT ... ON CONFLICT DO NOTHING。
-- 脚本可以安全重复执行，不覆盖应用运行后产生的 Case、Trace 或政策记录。
\set ON_ERROR_STOP on

-- =============================================================================
-- 一、政策知识数据：与 data/knowledge/airline_mvp/policies.json 对齐
-- =============================================================================

INSERT INTO knowledge_documents (
    document_id, version, section, title, domain, document_type, authority,
    valid_from, valid_to, status, locale, content, source_locator, metadata_json
) VALUES
(
    'journey_irrop_2026', '2026-06-01', '4.2', '国内航班非自愿退改说明',
    'journey', 'policy', 'official_policy', '2026-06-01', NULL, 'active', 'zh-CN',
    '因承运人原因导致航班取消时，符合条件的旅客可以申请非自愿改签或非自愿退票，具体操作须由有权限的客服或客票系统完成。',
    '{"path":"data/knowledge/airline_mvp/policies.json","documentId":"journey_irrop_2026","section":"4.2"}',
    '{"synthetic":true,"datasetVersion":"airline-mvp-v1"}'
),
(
    'journey_irrop_2025', '2025-01-01', '4.1', '国内航班非自愿退改说明（历史版）',
    'journey', 'policy', 'official_policy', '2025-01-01', '2026-05-31', 'expired', 'zh-CN',
    '历史版本：航班取消后的非自愿退改按旧流程处理。',
    '{"path":"data/knowledge/airline_mvp/policies.json","documentId":"journey_irrop_2025","section":"4.1"}',
    '{"synthetic":true,"datasetVersion":"airline-mvp-v1"}'
),
(
    'ticket_status_2026', '2026-01-01', '2.1', '电子客票状态说明',
    'ticketing', 'faq', 'approved_faq', '2026-01-01', NULL, 'active', 'zh-CN',
    'OPEN 表示票联尚未使用或完成退票；REFUNDED 表示客票系统已经完成票证退票处理，但不等于银行资金已经入账。',
    '{"path":"data/knowledge/airline_mvp/policies.json","documentId":"ticket_status_2026","section":"2.1"}',
    '{"synthetic":true,"datasetVersion":"airline-mvp-v1"}'
),
(
    'refund_timeline_2026', '2026-06-01', '3.2', '退款处理阶段与到账时效说明',
    'refund', 'policy', 'official_policy', '2026-06-01', NULL, 'active', 'zh-CN',
    '退款状态 PROCESSING 表示退款仍在航司、收单机构或发卡行链路处理中。客服不得承诺具体到账日期，应以支付渠道和银行最终入账为准。',
    '{"path":"data/knowledge/airline_mvp/policies.json","documentId":"refund_timeline_2026","section":"3.2"}',
    '{"synthetic":true,"datasetVersion":"airline-mvp-v1"}'
),
(
    'refund_missing_2026', '2026-06-01', '5.1', '未找到退款申请的处理方式',
    'refund', 'procedure', 'official_policy', '2026-06-01', NULL, 'active', 'zh-CN',
    '客票仍为 OPEN 且未找到退款申请时，应转交有权限的人工客服核对并提交相应退票操作。',
    '{"path":"data/knowledge/airline_mvp/policies.json","documentId":"refund_missing_2026","section":"5.1"}',
    '{"synthetic":true,"datasetVersion":"airline-mvp-v1"}'
),
(
    'payment_gateway_2026', '2026-01-01', '2.4', '支付渠道退款状态说明',
    'payment', 'faq', 'approved_faq', '2026-01-01', NULL, 'active', 'zh-CN',
    'ACQUIRER_PROCESSING 表示收单机构仍在处理退款，航司系统无法确认发卡行的最终入账时间。',
    '{"path":"data/knowledge/airline_mvp/policies.json","documentId":"payment_gateway_2026","section":"2.4"}',
    '{"synthetic":true,"datasetVersion":"airline-mvp-v1"}'
),
(
    'handoff_2026', '2026-01-01', '1.1', '人工接管规则',
    'refund', 'procedure', 'official_policy', '2026-01-01', NULL, 'active', 'zh-CN',
    '凡涉及改签、退票、退款发起、补偿发放等业务写操作，智能客服只能完成调查与说明，必须转有权限的人工客服执行。',
    '{"path":"data/knowledge/airline_mvp/policies.json","documentId":"handoff_2026","section":"1.1"}',
    '{"synthetic":true,"datasetVersion":"airline-mvp-v1"}'
),
(
    'baggage_delay_2026', '2026-01-01', '6.1', '行李延误查询说明',
    'baggage', 'policy', 'official_policy', '2026-01-01', NULL, 'active', 'zh-CN',
    '行李延误需要根据行李工单号查询最新追踪状态；涉及赔付时必须转人工审核材料。',
    '{"path":"data/knowledge/airline_mvp/policies.json","documentId":"baggage_delay_2026","section":"6.1"}',
    '{"synthetic":true,"datasetVersion":"airline-mvp-v1"}'
)
ON CONFLICT(document_id, version, section) DO NOTHING;

-- =============================================================================
-- 二、已经完成的 Journey + Refund 跨域调查 Case
-- =============================================================================

INSERT INTO conversations (
    conversation_id, verified_subject_id, locale, created_at, updated_at
) VALUES (
    'seed_conv_answered', 'subject_demo', 'zh-CN',
    '2026-07-29T00:30:00+00:00', '2026-07-29T00:31:00+00:00'
) ON CONFLICT(conversation_id) DO NOTHING;

INSERT INTO messages (
    message_id, conversation_id, role, content, request_id, created_at
) VALUES (
    'seed_msg_answered_001', 'seed_conv_answered', 'user',
    '我的航班 CZ3101 取消了，PNR 是 AB12CD，TKT1001 的退款为什么还没到账？',
    'seed_req_answered', '2026-07-29T00:30:00+00:00'
) ON CONFLICT(message_id) DO NOTHING;

INSERT INTO cases (
    case_id, conversation_id, request_id, status, user_goal, case_summary,
    plan_json, created_at, updated_at
) VALUES (
    'seed_case_answered', 'seed_conv_answered', 'seed_req_answered', 'responded',
    '确认航班取消情况，并解释退款尚未到账的原因',
    'CZ3101 已取消；TKT1001 票联已退，退款 RF1001 仍处于收单机构处理中。',
    '{"case_type":"journey_refund","user_goal":"确认航班取消情况，并解释退款尚未到账的原因","intents":["flight_disruption","refund_status"],"missing_fields":[],"tasks":[{"task_id":"seed_task_journey","domain":"journey","objective":"核实航班与客票状态","entity_refs":{"flight_no":"CZ3101","travel_date":"2026-07-29","pnr_ref":"AB12CD"},"allowed_tools":["get_flight_status","get_booking","get_ticket_status","search_airline_knowledge","get_policy_clause"],"required_evidence":["flight","booking","ticket","policy"],"max_tool_calls":6},{"task_id":"seed_task_refund","domain":"refund","objective":"核实退款与支付渠道状态","entity_refs":{"pnr_ref":"AB12CD","refund_ref":"RF1001"},"allowed_tools":["get_payment_status","get_refund_status","search_airline_knowledge","get_policy_clause"],"required_evidence":["refund","payment","policy"],"max_tool_calls":4}],"parallel":true,"human_action_likely":false}',
    '2026-07-29T00:30:00+00:00', '2026-07-29T00:31:00+00:00'
) ON CONFLICT(case_id) DO NOTHING;

INSERT INTO tool_calls (
    tool_call_id, case_id, invocation_id, task_id, domain, tool_name,
    arguments_json, status, error_code, started_at, ended_at
) VALUES
(
    'seed_tc_flight', 'seed_case_answered', 'seed_inv_journey',
    'seed_task_journey', 'journey', 'get_flight_status',
    '{"flight_no":"CZ3101","date":"2026-07-29"}', 'success', NULL,
    '2026-07-29T00:30:10+00:00', '2026-07-29T00:30:10.035+00:00'
),
(
    'seed_tc_refund', 'seed_case_answered', 'seed_inv_refund',
    'seed_task_refund', 'refund', 'get_refund_status',
    '{"refund_ref":"RF1001"}', 'success', NULL,
    '2026-07-29T00:30:10+00:00', '2026-07-29T00:30:10.041+00:00'
),
(
    'seed_tc_policy', 'seed_case_answered', 'seed_inv_refund',
    'seed_task_refund', 'refund', 'get_policy_clause',
    '{"document_id":"refund_timeline_2026","version":"2026-06-01","section":"3.2"}',
    'success', NULL,
    '2026-07-29T00:30:11+00:00', '2026-07-29T00:30:11.019+00:00'
)
ON CONFLICT(tool_call_id) DO NOTHING;

INSERT INTO evidence_items (
    evidence_id, case_id, source_type, source_id, authority, version,
    payload_json, created_at
) VALUES
(
    'seed_ev_flight', 'seed_case_answered', 'fixture_get_flight_status',
    'CZ3101:2026-07-29', 'system_of_record', 'airline-mvp-v1',
    '{"evidence_id":"seed_ev_flight","case_id":"seed_case_answered","evidence_type":"flight","source_type":"fixture_get_flight_status","source_id":"CZ3101:2026-07-29","authority":"system_of_record","summary":"CZ3101 在 2026-07-29 的状态为 CANCELLED","structured_data":{"status":"CANCELLED","reasonCategory":"CARRIER_CONTROLLED"},"observed_at":"2026-07-29T00:30:10+00:00","valid_from":null,"valid_to":null,"version":"airline-mvp-v1","locator":{"fixture":"flights.json","flightNo":"CZ3101"},"confidence":1.0}',
    '2026-07-29T00:30:10+00:00'
),
(
    'seed_ev_refund', 'seed_case_answered', 'fixture_get_refund_status',
    'RF1001', 'system_of_record', 'airline-mvp-v1',
    '{"evidence_id":"seed_ev_refund","case_id":"seed_case_answered","evidence_type":"refund","source_type":"fixture_get_refund_status","source_id":"RF1001","authority":"system_of_record","summary":"退款 RF1001 仍在处理","structured_data":{"refundStatus":"PROCESSING","stage":"ACQUIRING_BANK"},"observed_at":"2026-07-29T00:30:10+00:00","valid_from":null,"valid_to":null,"version":"airline-mvp-v1","locator":{"fixture":"refunds.json","refundRef":"RF1001"},"confidence":1.0}',
    '2026-07-29T00:30:10+00:00'
),
(
    'seed_ev_policy', 'seed_case_answered', 'policy_document',
    'refund_timeline_2026:3.2', 'official_policy', '2026-06-01',
    '{"evidence_id":"seed_ev_policy","case_id":"seed_case_answered","evidence_type":"policy","source_type":"policy_document","source_id":"refund_timeline_2026:3.2","authority":"official_policy","summary":"PROCESSING 不代表已经到账，客服不能承诺具体到账时间","structured_data":{"documentId":"refund_timeline_2026","section":"3.2"},"observed_at":"2026-07-29T00:30:11+00:00","valid_from":"2026-06-01","valid_to":null,"version":"2026-06-01","locator":{"path":"data/knowledge/airline_mvp/policies.json","section":"3.2"},"confidence":1.0}',
    '2026-07-29T00:30:11+00:00'
)
ON CONFLICT(evidence_id) DO NOTHING;

INSERT INTO service_responses (
    response_id, case_id, response_version, payload_json, created_at
) VALUES (
    'seed_resp_answered_v1', 'seed_case_answered', 1,
    '{"response_status":"answered","answer":"已核实：CZ3101 于 2026 年 7 月 29 日取消。TKT1001 的票证退票已完成，但退款 RF1001 仍处于收单机构处理阶段，因此目前不能确认银行最终入账时间。","verified_facts":[{"statement":"CZ3101 已取消","evidence_ids":["seed_ev_flight"],"confidence":1.0,"uncertainty":null},{"statement":"RF1001 仍处于收单机构处理阶段","evidence_ids":["seed_ev_refund","seed_ev_policy"],"confidence":1.0,"uncertainty":"银行最终入账时间未知"}],"available_options":[],"missing_information":[],"handoff_required":false,"handoff_reason":null,"must_not_claim":["退款已经到账","承诺具体到账日期"]}',
    '2026-07-29T00:31:00+00:00'
) ON CONFLICT(case_id, response_version) DO NOTHING;

INSERT INTO trace_events (
    event_id, trace_id, case_id, event_type, sequence_no, payload_json, created_at
) VALUES
('seed_trace_answered_001', 'seed_trace_answered', 'seed_case_answered', 'request.received', 1,
 '{"requestId":"seed_req_answered","conversationId":"seed_conv_answered","messageLength":49}', '2026-07-29T00:30:00+00:00'),
('seed_trace_answered_002', 'seed_trace_answered', 'seed_case_answered', 'coordinator.planned', 2,
 '{"intents":["flight_disruption","refund_status"],"tasks":[{"taskId":"seed_task_journey","domain":"journey"},{"taskId":"seed_task_refund","domain":"refund"}],"parallel":true}', '2026-07-29T00:30:02+00:00'),
('seed_trace_answered_003', 'seed_trace_answered', 'seed_case_answered', 'tool.completed', 3,
 '{"toolName":"get_flight_status","toolCallId":"seed_tc_flight","status":"success","evidenceId":"seed_ev_flight"}', '2026-07-29T00:30:10+00:00'),
('seed_trace_answered_004', 'seed_trace_answered', 'seed_case_answered', 'tool.completed', 4,
 '{"toolName":"get_refund_status","toolCallId":"seed_tc_refund","status":"success","evidenceId":"seed_ev_refund"}', '2026-07-29T00:30:10.050+00:00'),
('seed_trace_answered_005', 'seed_trace_answered', 'seed_case_answered', 'quality.checked', 5,
 '{"decision":"pass","violations":[],"invalidEvidenceIds":[]}', '2026-07-29T00:30:59+00:00'),
('seed_trace_answered_006', 'seed_trace_answered', 'seed_case_answered', 'case.completed', 6,
 '{"status":"responded","toolCallCount":3,"evidenceCount":3,"handoffId":null}', '2026-07-29T00:31:00+00:00')
ON CONFLICT(case_id, sequence_no) DO NOTHING;

-- =============================================================================
-- 三、等待人工执行写操作的 Case：展示只读 Agent 的权限边界
-- =============================================================================

INSERT INTO conversations (
    conversation_id, verified_subject_id, locale, created_at, updated_at
) VALUES (
    'seed_conv_handoff', 'subject_demo', 'zh-CN',
    '2026-07-29T01:00:00+00:00', '2026-07-29T01:01:00+00:00'
) ON CONFLICT(conversation_id) DO NOTHING;

INSERT INTO messages (
    message_id, conversation_id, role, content, request_id, created_at
) VALUES (
    'seed_msg_handoff_001', 'seed_conv_handoff', 'user',
    'TKT1002 还没有退款记录，请现在直接帮我退票。',
    'seed_req_handoff', '2026-07-29T01:00:00+00:00'
) ON CONFLICT(message_id) DO NOTHING;

INSERT INTO cases (
    case_id, conversation_id, request_id, status, user_goal, case_summary,
    plan_json, created_at, updated_at
) VALUES (
    'seed_case_handoff', 'seed_conv_handoff', 'seed_req_handoff',
    'waiting_for_human', '为 TKT1002 发起退票',
    '系统只完成只读调查；退票属于写操作，已排队转人工处理。',
    '{"case_type":"refund_write_request","user_goal":"为 TKT1002 发起退票","intents":["refund_request"],"missing_fields":[],"tasks":[{"task_id":"seed_task_handoff_refund","domain":"refund","objective":"核实退款记录与转人工政策","entity_refs":{"ticket_ref":"TKT1002"},"allowed_tools":["get_refund_status","search_airline_knowledge","get_policy_clause"],"required_evidence":["refund","policy"],"max_tool_calls":4}],"parallel":false,"human_action_likely":true}',
    '2026-07-29T01:00:00+00:00', '2026-07-29T01:01:00+00:00'
) ON CONFLICT(case_id) DO NOTHING;

INSERT INTO tool_calls (
    tool_call_id, case_id, invocation_id, task_id, domain, tool_name,
    arguments_json, status, error_code, started_at, ended_at
) VALUES (
    'seed_tc_handoff_refund', 'seed_case_handoff', 'seed_inv_handoff_refund',
    'seed_task_handoff_refund', 'refund', 'get_refund_status',
    '{"ticket_ref":"TKT1002"}', 'not_found', NULL,
    '2026-07-29T01:00:10+00:00', '2026-07-29T01:00:10.030+00:00'
) ON CONFLICT(tool_call_id) DO NOTHING;

INSERT INTO evidence_items (
    evidence_id, case_id, source_type, source_id, authority, version,
    payload_json, created_at
) VALUES (
    'seed_ev_handoff_policy', 'seed_case_handoff', 'policy_document',
    'handoff_2026:1.1', 'official_policy', '2026-01-01',
    '{"evidence_id":"seed_ev_handoff_policy","case_id":"seed_case_handoff","evidence_type":"policy","source_type":"policy_document","source_id":"handoff_2026:1.1","authority":"official_policy","summary":"退票属于写操作，必须由有权限的人工客服执行","structured_data":{"documentId":"handoff_2026","section":"1.1"},"observed_at":"2026-07-29T01:00:11+00:00","valid_from":"2026-01-01","valid_to":null,"version":"2026-01-01","locator":{"path":"data/knowledge/airline_mvp/policies.json","section":"1.1"},"confidence":1.0}',
    '2026-07-29T01:00:11+00:00'
) ON CONFLICT(evidence_id) DO NOTHING;

INSERT INTO service_responses (
    response_id, case_id, response_version, payload_json, created_at
) VALUES (
    'seed_resp_handoff_v1', 'seed_case_handoff', 1,
    '{"response_status":"handoff_required","answer":"目前没有找到 TKT1002 的退款申请。退票属于写操作，智能客服不能代替客票系统执行，已为你转交人工客服继续处理。","verified_facts":[],"available_options":[{"option":"由人工客服核验并发起符合条件的退票","execution_status":"not_executed","evidence_ids":["seed_ev_handoff_policy"]}],"missing_information":[],"handoff_required":true,"handoff_reason":"write_action_requires_human","must_not_claim":["退票已完成","退款已经发起"]}',
    '2026-07-29T01:00:59+00:00'
) ON CONFLICT(case_id, response_version) DO NOTHING;

INSERT INTO handoffs (
    handoff_id, case_id, reason_code, response_version, target_queue,
    status, payload_json, created_at
) VALUES (
    'seed_handoff_refund_001', 'seed_case_handoff',
    'write_action_requires_human', 1, 'refund_operations', 'queued',
    '{"case_id":"seed_case_handoff","reason_code":"write_action_requires_human","target_queue":"refund_operations","priority":"normal","customer_request":"为 TKT1002 发起退票","verified_fact_refs":["seed_ev_handoff_policy"],"unresolved_items":["需要人工核验退票条件并执行写操作"],"conversation_cursor":"seed_conv_handoff","status":"queued","handoff_id":"seed_handoff_refund_001"}',
    '2026-07-29T01:01:00+00:00'
) ON CONFLICT(case_id, reason_code, response_version) DO NOTHING;

INSERT INTO trace_events (
    event_id, trace_id, case_id, event_type, sequence_no, payload_json, created_at
) VALUES
('seed_trace_handoff_001', 'seed_trace_handoff', 'seed_case_handoff', 'request.received', 1,
 '{"requestId":"seed_req_handoff","conversationId":"seed_conv_handoff","messageLength":25}', '2026-07-29T01:00:00+00:00'),
('seed_trace_handoff_002', 'seed_trace_handoff', 'seed_case_handoff', 'quality.checked', 2,
 '{"decision":"handoff","violations":[],"invalidEvidenceIds":[]}', '2026-07-29T01:00:59+00:00'),
('seed_trace_handoff_003', 'seed_trace_handoff', 'seed_case_handoff', 'handoff.queued', 3,
 '{"handoffId":"seed_handoff_refund_001","reasonCode":"write_action_requires_human","targetQueue":"refund_operations"}', '2026-07-29T01:01:00+00:00'),
('seed_trace_handoff_004', 'seed_trace_handoff', 'seed_case_handoff', 'case.completed', 4,
 '{"status":"waiting_for_human","toolCallCount":1,"evidenceCount":1,"handoffId":"seed_handoff_refund_001"}', '2026-07-29T01:01:00.010+00:00')
ON CONFLICT(case_id, sequence_no) DO NOTHING;
