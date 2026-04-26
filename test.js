
(function() {
    var baseUrl = '/prd_audit';
    var outputEl = document.getElementById('markdownRenderArea'); // 指向新的 Markdown 渲染区
    var levelContextBadgeEl = document.getElementById('levelContextBadge');
    var levelContextDescEl = document.getElementById('levelContextDesc');
    var featureMapMainWrapEl = document.getElementById('mainFeatureMapWrap');
    var featureMapMainEl = document.getElementById('featureMapMainContent');
    var dashboardEl = document.getElementById('auditDashboard');
    var cardAreaEl = document.getElementById('defectCardArea');
    var modeSwitchEl = document.getElementById('reportModeSwitch');
    
    var inputEl = document.getElementById('inputContent');
    console.log('Initial inputEl check:', inputEl);

    var btnGenerate = document.getElementById('btnGenerate');
    var statusText = document.getElementById('statusText');
    var btnCopy = document.getElementById('btnCopy');
    var btnDownloadMd = document.getElementById('btnDownloadMd');
    var btnDownloadWord = document.getElementById('btnDownloadWord');
    var btnSaveCases = document.getElementById('btnSaveCases');
    var docUpload = document.getElementById('docUpload');
    var bugCsvUpload = document.getElementById('bugCsvUpload');
    var useLLMCheckbox = document.getElementById('useLLM');
    
    var lastReport = '';
    var reportLevel = 'OUTLINE';
    var reports = { OUTLINE: '', L1: '', L2: '', L3: '' };
    var summaryData = null; // 结构化总览
    var scanMetaData = null; // Stage2 LLM scan_meta（llm_scan_ok / llm_error 等）
    var defectsData = [];   // 结构化漏洞列表
    var currentMode = 'doc'; // 'doc' or 'card'

    var testMatrix = null;
    var diagrams = null;
    var extrasQuality = null;
    var kg = null;
    var outlineEngine = null;
    var platformImpact = null;
    var dependencyAnalysis = null;
    var prdQuality = null;
    var testPoints = null;
    var validationOutline = null;  // 验证大纲数据
    var riskPrediction = null;
    var understandingCards = null;
    var releaseGate = null;
    var sharedSummary = null;
    var readerGuide = null;
    var activeLinkedTestPointId = '';
    var matrixFilters = { missing: false, p0: false };
    var parseMeta = null;
    var featureMindmapCode = '';
    var knowledgeCards = [];
    var editingKnowledgeIndex = -1;
    var explicitOutlineLatest = null;
    var explicitRoleRowsLatest = [];
    /** @type {object|null} 最近一次 /api/outline_llm 完整 data（含 llm、stage1_output、可选 merged） */
    var llmFourPillarsPayload = null;

    var extraWrap = document.getElementById('extraWrap');
    var matrixEl = document.getElementById('matrixContent');
    var testPointsEl = document.getElementById('testPointsContent');
    var diagramEl = document.getElementById('diagramContent');
    var extrasQualityEl = document.getElementById('extrasQualityBadges');
    var kgEl = document.getElementById('kgContent');
    var qualityEl = document.getElementById('qualityContent');
    var outlineEl = document.getElementById('outlineContent');
    var parseEl = document.getElementById('parseContent');
    var featureMapEl = document.getElementById('featureMapContent');
    var featureMapMainMetaEl = document.getElementById('featureMapMainMeta');
    var contentOutlineMainWrapEl = document.getElementById('contentOutlineMainWrap');
    var llmFourPillarsWrapEl = document.getElementById('llmFourPillarsWrap');
    var llmFourPillarsBodyEl = document.getElementById('llmFourPillarsBody');
    var llmOutlineStatusEl = document.getElementById('llmOutlineStatus');
    var fourPillarsAuditWrapEl = document.getElementById('fourPillarsAuditWrap');
    var fourPillarsAuditContentEl = document.getElementById('fourPillarsAuditContent');
    var sharedSummaryMainWrapEl = document.getElementById('sharedSummaryMainWrap');
    var sharedSummaryMainContentEl = document.getElementById('sharedSummaryMainContent');
    var contentOutlineMainMetaEl = document.getElementById('contentOutlineMainMeta');
    var contentOutlineMainContentEl = document.getElementById('contentOutlineMainContent');
    var impactEl = document.getElementById('impactContent');
    var dependencyEl = document.getElementById('dependencyContent');
    var riskPredictionEl = document.getElementById('riskPredictionContent');
    var understandingCardsEl = document.getElementById('understandingCardsContent');
    var releaseGateEl = document.getElementById('releaseGateContent');
    var decisionPanelMainEl = document.getElementById('decisionPanelMain');
    var readerGuideMainWrapEl = document.getElementById('readerGuideMainWrap');
    var readerGuideMainContentEl = document.getElementById('readerGuideMainContent');
    var platformImpactMainWrapEl = document.getElementById('platformImpactMainWrap');
    var platformImpactMainContentEl = document.getElementById('platformImpactMainContent');
    var knowledgeEl = document.getElementById('knowledgeContent');
    var bugInputEl = document.getElementById('bugInputContent');
    var bugStatusEl = document.getElementById('bugStatusText');
    var bugResultEl = document.getElementById('bugResultContent');
    var btnJiraPreview = document.getElementById('btnJiraPreview');
    var btnJiraImport = document.getElementById('btnJiraImport');
    var btnVoiceInput = document.getElementById('btnVoiceInput');

    // 语音输入逻辑 (Web Speech API)
    if (btnVoiceInput) {
        var isRecording = false;
        var recognition = null;
        if ('webkitSpeechRecognition' in window || 'SpeechRecognition' in window) {
            var SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
            recognition = new SpeechRecognition();
            recognition.continuous = true;
            recognition.interimResults = true;
            recognition.lang = 'zh-CN';

            recognition.onstart = function() {
                isRecording = true;
                btnVoiceInput.classList.remove('btn-outline-info');
                btnVoiceInput.classList.add('btn-danger');
                btnVoiceInput.innerHTML = '<i class="fas fa-stop-circle me-1"></i> 停止录音';
                showNonBlockingToast('开始语音输入，请对着麦克风说话...', 'info');
            };

            recognition.onresult = function(event) {
                var interimTranscript = '';
                var finalTranscript = '';
                for (var i = event.resultIndex; i < event.results.length; ++i) {
                    if (event.results[i].isFinal) {
                        finalTranscript += event.results[i][0].transcript;
                    } else {
                        interimTranscript += event.results[i][0].transcript;
                    }
                }
                
                var targetInput = document.getElementById('inputContent') || inputEl;
                if (targetInput && finalTranscript) {
                    var currentVal = targetInput.value;
                    targetInput.value = currentVal + (currentVal && !currentVal.endsWith('\n') ? '\n' : '') + finalTranscript;
                    targetInput.scrollTop = targetInput.scrollHeight; // 滚动到底部
                }
            };

            recognition.onerror = function(event) {
                console.error('Speech recognition error:', event.error);
                showNonBlockingToast('语音识别错误: ' + event.error, 'danger');
                stopRecording();
            };

            recognition.onend = function() {
                stopRecording();
            };
        }

        function stopRecording() {
            if (!isRecording) return;
            isRecording = false;
            if (recognition) recognition.stop();
            btnVoiceInput.classList.remove('btn-danger');
            btnVoiceInput.classList.add('btn-outline-info');
            btnVoiceInput.innerHTML = '<i class="fas fa-microphone me-1"></i> 语音输入';
            showNonBlockingToast('语音输入已结束', 'success');
        }

        btnVoiceInput.addEventListener('click', function() {
            if (!recognition) {
                alert('抱歉，您的浏览器不支持语音识别功能，请使用 Chrome 或 Edge 浏览器。');
                return;
            }
            if (isRecording) {
                stopRecording();
            } else {
                try {
                    recognition.start();
                } catch(e) {
                    console.error(e);
                    showNonBlockingToast('无法启动麦克风，请检查权限设置', 'danger');
                }
            }
        });
    }

    // 如果初始化时没拿到，尝试在 500ms 后再次获取（应对某些动态渲染情况）
    if (!inputEl) {
        setTimeout(function() {
            inputEl = document.getElementById('inputContent');
            console.log('Delayed inputEl check:', inputEl);
        }, 500);
    }
    var llmProviderInput = document.getElementById('llmProvider');
    var llmBaseUrlInput = document.getElementById('llmBaseUrl');
    var llmModelInput = document.getElementById('llmModel');
    var llmApiKeyInput = document.getElementById('llmApiKey');
    var llmFallbackEnabledInput = document.getElementById('llmFallbackEnabled');
    var llmFallbackBaseUrlInput = document.getElementById('llmFallbackBaseUrl');
    var llmFallbackModelInput = document.getElementById('llmFallbackModel');
    var llmConfigStatus = document.getElementById('llmConfigStatus');
    var btnMatrixTopEntry = document.getElementById('btnMatrixTopEntry');
    if (btnMatrixTopEntry) {
        btnMatrixTopEntry.addEventListener('click', function() {
            window.open('/prd_audit/matrix_view', '_blank');
        });
    }
    
    var btnMatrixFullView = document.getElementById('btnMatrixFullView');
    var btnMatrixStandalone = document.getElementById('btnMatrixStandalone');
    var btnExportTestPoints = document.getElementById('btnExportTestPoints');
    var matrixFullViewModal = new bootstrap.Modal(document.getElementById('matrixFullViewModal'));
    var matrixFullContentEl = document.getElementById('matrixFullContent');
    var btnRefreshKnowledge = document.getElementById('btnRefreshKnowledge');
    var btnKnowledgeAdd = document.getElementById('btnKnowledgeAdd');
    var btnKnowledgeImport = document.getElementById('btnKnowledgeImport');
    var btnKnowledgeExport = document.getElementById('btnKnowledgeExport');
    var btnKnowledgeSaveAll = document.getElementById('btnKnowledgeSaveAll');
    var knowledgeImportInput = document.getElementById('knowledgeImportInput');
    var knowledgeCardModalEl = document.getElementById('knowledgeCardModal');
    var knowledgeCardModal = knowledgeCardModalEl ? new bootstrap.Modal(knowledgeCardModalEl) : null;
    var knowledgeCardModalTitle = document.getElementById('knowledgeCardModalTitle');
    var knowledgeCardJson = document.getElementById('knowledgeCardJson');
    var knowledgeCardStatus = document.getElementById('knowledgeCardStatus');
    var btnKnowledgeCardSave = document.getElementById('btnKnowledgeCardSave');
    var btnDownloadFeatureMap = document.getElementById('btnDownloadFeatureMap');
    var btnExportXmind = document.getElementById('btnExportXmind');
    var btnBugImport = document.getElementById('btnBugImport');
    var btnBugAnalyze = document.getElementById('btnBugAnalyze');
    var btnPrdAuditByBug = document.getElementById('btnPrdAuditByBug');
    var globalToastEl = document.getElementById('globalToast');
    var globalToastBodyEl = document.getElementById('globalToastBody');
    var globalToast = (globalToastEl && window.bootstrap && window.bootstrap.Toast) ? new window.bootstrap.Toast(globalToastEl) : null;
    var MATRIX_SNAPSHOT_KEY = 'prd_audit_matrix_bundle';

    // 历史记录相关逻辑
    var historyModalEl = document.getElementById('historySnapshotsModal');
    var historyCompareHintEl = document.getElementById('historyCompareHint');
    var btnCompareSnapshots = document.getElementById('btnCompareSnapshots');
    var historyCompareModalEl = document.getElementById('historyCompareModal');
    var historyCompareModal = historyCompareModalEl ? new bootstrap.Modal(historyCompareModalEl) : null;
    var historyCompareContentEl = document.getElementById('historyCompareContent');
    var btnExportHistoryCompare = document.getElementById('btnExportHistoryCompare');
    var historyCompareSelection = [];
    var historySnapshotsCache = [];
    var latestHistoryCompareExport = null;

    function toHistoryLabel(s) {
        var dateStr = s.created_at_str || new Date((s.created_at || 0) * 1000).toLocaleString();
        var preview = (s.preview && typeof s.preview === 'object') ? s.preview : {};
        var productText = preview.product_name || s.snapshot_id || '';
        return dateStr + ' · ' + productText;
    }

    function updateHistoryCompareUi() {
        if (btnCompareSnapshots) {
            btnCompareSnapshots.disabled = historyCompareSelection.length !== 2;
            btnCompareSnapshots.innerHTML = '<i class="fas fa-code-compare me-1"></i>对比 ' + historyCompareSelection.length + ' / 2';
        }
        if (historyCompareHintEl) {
            if (historyCompareSelection.length === 0) historyCompareHintEl.textContent = '可勾选两次记录进行对比';
            else if (historyCompareSelection.length === 1) historyCompareHintEl.textContent = '已选择 1 条，再选 1 条即可对比';
            else historyCompareHintEl.textContent = '已选择两条记录，可直接查看差异';
        }
    }

    function toggleHistoryCompareSelection(snapshotId) {
        var sid = String(snapshotId || '');
        if (!sid) return;
        var idx = historyCompareSelection.indexOf(sid);
        if (idx >= 0) {
            historyCompareSelection.splice(idx, 1);
        } else {
            if (historyCompareSelection.length >= 2) {
                showNonBlockingToast('最多只能选择两条历史记录进行对比', 'danger');
                return;
            }
            historyCompareSelection.push(sid);
        }
        renderHistorySnapshotsList(historySnapshotsCache);
    }

    function renderHistorySnapshotsList(snaps) {
        var listEl = document.getElementById('historySnapshotsList');
        historySnapshotsCache = Array.isArray(snaps) ? snaps.slice() : [];
        if (!listEl) return;
        updateHistoryCompareUi();
        if (!historySnapshotsCache.length) {
            listEl.innerHTML = '<div class="p-4 text-center text-muted small">暂无历史记录</div>';
            return;
        }
        var html = '';
        historySnapshotsCache.forEach(function(s) {
            var dateStr = s.created_at_str || new Date(s.created_at * 1000).toLocaleString();
            var preview = (s.preview && typeof s.preview === 'object') ? s.preview : {};
            var isSelected = historyCompareSelection.indexOf(String(s.snapshot_id || '')) >= 0;
            var modeBadge = s.offline_mode ? '<span class="badge bg-secondary ms-2">本地模式</span>' : '<span class="badge bg-primary ms-2">AI 模式</span>';
            var riskBadge = '';
            if (s.p0_count > 0) riskBadge += '<span class="badge bg-danger ms-1">P0: ' + s.p0_count + '</span>';
            if (s.p1_count > 0) riskBadge += '<span class="badge bg-warning text-dark ms-1">P1: ' + s.p1_count + '</span>';
            if (preview.quality_score != null && preview.quality_score !== '') {
                var q = Number(preview.quality_score);
                var qText = isNaN(q) ? String(preview.quality_score) : (Math.round(q * 10) / 10);
                riskBadge += '<span class="badge bg-light text-dark border ms-1">质量 ' + escapeHtml(String(qText)) + '</span>';
            }
            var assetBadges = '';
            if (preview.has_llm_outline) assetBadges += '<span class="badge bg-info-subtle text-info-emphasis border ms-1">认知大纲</span>';
            if (preview.has_test_matrix) assetBadges += '<span class="badge bg-success-subtle text-success-emphasis border ms-1">测试矩阵</span>';
            if (preview.has_kg) assetBadges += '<span class="badge bg-primary-subtle text-primary-emphasis border ms-1">知识图谱</span>';
            if (preview.has_dependency_analysis) assetBadges += '<span class="badge bg-warning-subtle text-warning-emphasis border ms-1">依赖分析</span>';
            if (preview.has_understanding_cards) assetBadges += '<span class="badge bg-secondary-subtle text-secondary-emphasis border ms-1">理解卡片 ' + escapeHtml(String(preview.understanding_card_count || 0)) + '</span>';
            var summaryText = preview.main_problem || '';
            var productText = preview.product_name || s.snapshot_id || '';
            var laneText = s.lane || preview.lane || 'none';
            var sourceText = '规则 ' + (s.source_rule_count || preview.source_rule_count || 0) + ' / LLM ' + (s.source_llm_count || preview.source_llm_count || 0);
            var moduleText = Array.isArray(preview.top_modules) && preview.top_modules.length ? preview.top_modules.join('、') : '';

            html += '<div class="list-group-item' + (isSelected ? ' border-primary bg-primary-subtle bg-opacity-25' : '') + '">';
            html += '<div class="d-flex align-items-start gap-3">';
            html += '<div class="form-check mt-1">';
            html += '<input class="form-check-input history-compare-check" type="checkbox" data-id="' + escapeHtml(s.snapshot_id) + '"' + (isSelected ? ' checked' : '') + '>';
            html += '</div>';
            html += '<div class="flex-grow-1 min-w-0">';
            html += '<div class="d-flex w-100 justify-content-between flex-wrap gap-2">';
            html += '<h6 class="mb-1">' + escapeHtml(dateStr) + modeBadge + riskBadge + '</h6>';
            html += '<small class="text-muted">缺陷: ' + (s.defects_count || 0) + ' · 分轨: ' + escapeHtml(String(laneText)) + '</small>';
            html += '</div>';
            html += '<div class="mb-1 small fw-semibold text-dark text-truncate" style="max-width: 92%;">' + escapeHtml(productText) + '</div>';
            html += '<div class="mb-1 small text-muted text-truncate" style="max-width: 92%;">' + escapeHtml(s.snapshot_id) + '</div>';
            html += '<div class="mb-1 small text-muted">命中来源：' + escapeHtml(sourceText) + (moduleText ? ' · 重点模块：' + escapeHtml(moduleText) : '') + '</div>';
            if (assetBadges) html += '<div class="mb-1">' + assetBadges + '</div>';
            if (summaryText) html += '<p class="mb-0 small text-muted text-truncate" style="max-width: 92%;">核心问题：' + escapeHtml(summaryText) + '</p>';
            html += '</div>';
            html += '<div class="d-flex flex-column gap-2">';
            html += '<button type="button" class="btn btn-sm btn-outline-primary btn-load-snapshot" data-id="' + escapeHtml(s.snapshot_id) + '">加载</button>';
            html += '<button type="button" class="btn btn-sm ' + (isSelected ? 'btn-primary' : 'btn-outline-secondary') + ' btn-toggle-compare" data-id="' + escapeHtml(s.snapshot_id) + '">' + (isSelected ? '取消对比' : '加入对比') + '</button>';
            html += '</div>';
            html += '</div>';
            html += '</div>';
        });
        listEl.innerHTML = html;

        listEl.querySelectorAll('.btn-load-snapshot').forEach(function(btn) {
            btn.addEventListener('click', function(e) {
                e.preventDefault();
                var sid = this.getAttribute('data-id');
                loadSnapshotDetail(sid);
            });
        });
        listEl.querySelectorAll('.btn-toggle-compare').forEach(function(btn) {
            btn.addEventListener('click', function(e) {
                e.preventDefault();
                toggleHistoryCompareSelection(this.getAttribute('data-id'));
            });
        });
        listEl.querySelectorAll('.history-compare-check').forEach(function(checkbox) {
            checkbox.addEventListener('change', function() {
                toggleHistoryCompareSelection(this.getAttribute('data-id'));
            });
        });
    }

    function historyBoolBadge(v) {
        return v ? '<span class="badge bg-success-subtle text-success-emphasis border">有</span>' : '<span class="badge bg-light text-dark border">无</span>';
    }

    function historyValueText(v, fallback) {
        if (v === null || v === undefined || v === '') return fallback || '—';
        return escapeHtml(String(v));
    }

    function historyDeltaBadge(fromVal, toVal, reverseBetter) {
        var a = Number(fromVal);
        var b = Number(toVal);
        if (isNaN(a) || isNaN(b)) return '<span class="badge bg-light text-dark border">—</span>';
        var diff = Math.round((b - a) * 10) / 10;
        if (diff === 0) return '<span class="badge bg-light text-dark border">持平</span>';
        var positiveBetter = reverseBetter ? diff < 0 : diff > 0;
        var cls = positiveBetter ? 'bg-success-subtle text-success-emphasis' : 'bg-danger-subtle text-danger-emphasis';
        var sign = diff > 0 ? '+' : '';
        return '<span class="badge ' + cls + ' border">' + sign + diff + '</span>';
    }

    function historyNumberOrNull(v) {
        var n = Number(v);
        return isNaN(n) ? null : n;
    }

    function safeHistoryFilePart(v) {
        return String(v || '').replace(/[\\/:*?"<>|]+/g, '_').replace(/\s+/g, '_').slice(0, 40) || 'snapshot';
    }

    function buildHistoryCompareSummary(left, right, lp, rp, leftAssetCount, rightAssetCount) {
        var qualityLeft = historyNumberOrNull(lp.quality_score);
        var qualityRight = historyNumberOrNull(rp.quality_score);
        var qualityDiff = (qualityLeft === null || qualityRight === null) ? null : Math.round((qualityRight - qualityLeft) * 10) / 10;
        var p0Left = historyNumberOrNull(left.p0_count);
        var p0Right = historyNumberOrNull(right.p0_count);
        var p0Diff = (p0Left === null || p0Right === null) ? null : p0Right - p0Left;
        var assetDiff = rightAssetCount - leftAssetCount;
        var summary = {
            toneClass: 'alert-secondary',
            title: '两次审计结果整体接近，建议继续关注关键风险和资产完整度。',
            items: [],
            actions: []
        };

        if (qualityDiff !== null) {
            if (qualityDiff > 0) summary.items.push('质量分从 ' + qualityLeft + ' 提升到 ' + qualityRight + '。');
            else if (qualityDiff < 0) summary.items.push('质量分从 ' + qualityLeft + ' 下降到 ' + qualityRight + '。');
            else summary.items.push('质量分保持在 ' + qualityRight + '，整体成熟度变化不大。');
        }
        if (p0Diff !== null) {
            if (p0Diff < 0) summary.items.push('P0 风险从 ' + p0Left + ' 个降到 ' + p0Right + ' 个，关键阻断问题有所收敛。');
            else if (p0Diff > 0) summary.items.push('P0 风险从 ' + p0Left + ' 个升到 ' + p0Right + ' 个，建议先回到需求澄清。');
            else summary.items.push('P0 风险保持 ' + p0Right + ' 个，高风险问题尚未发生结构性变化。');
        }
        if (assetDiff > 0) summary.items.push('本次额外补齐了 ' + assetDiff + ' 项审计资产，输出完整度更高。');
        else if (assetDiff < 0) summary.items.push('本次审计资产比上次少了 ' + Math.abs(assetDiff) + ' 项，建议检查是否有能力未生成。');
        else summary.items.push('审计资产数量与上次持平。');

        var leftProblem = String(lp.main_problem || '').trim();
        var rightProblem = String(rp.main_problem || '').trim();
        if (leftProblem && rightProblem && leftProblem !== rightProblem) {
            summary.items.push('核心问题从“' + leftProblem + '”转为“' + rightProblem + '”。');
        }
        if (!lp.has_llm_outline && rp.has_llm_outline) summary.items.push('对比版本补齐了认知大纲，更适合做业务评审与复盘。');
        if (!lp.has_test_matrix && rp.has_test_matrix) summary.items.push('对比版本新增了测试矩阵，验证视角更加完整。');

        if (p0Right !== null && p0Right > 0) {
            summary.actions.push('先处理剩余的 P0 问题，再继续推进评审、提测或排期。');
        }
        if (!rp.has_llm_outline) {
            summary.actions.push('补一版认知大纲，方便产品、研发、测试对齐同一份业务理解。');
        }
        if (!rp.has_test_matrix) {
            summary.actions.push('补测试矩阵或测试点，确保高风险链路可以被验证。');
        }
        if (qualityDiff !== null && qualityDiff < 0) {
            summary.actions.push('回看本次改动引入的新增风险，优先检查核心流程、异常分支和状态切换。');
        }
        if (assetDiff < 0) {
            summary.actions.push('检查本次是否有依赖分析、知识图谱或理解卡片未成功生成，避免资产回退。');
        }
        if (!summary.actions.length && qualityDiff !== null && qualityDiff > 0 && (p0Diff === null || p0Diff <= 0)) {
            summary.actions.push('可以进入下一轮细化，优先补齐待确认项并准备测试设计。');
        }
        if (!summary.actions.length) {
            summary.actions.push('继续围绕核心问题、重点模块和待确认项做定向收敛。');
        }

        if ((qualityDiff !== null && qualityDiff > 0) && (p0Diff !== null && p0Diff < 0)) {
            summary.toneClass = 'alert-success';
            summary.title = '本次 PRD 质量明显提升，关键风险和审计资产都在向好的方向收敛。';
        } else if ((p0Diff !== null && p0Diff > 0) || (qualityDiff !== null && qualityDiff < 0)) {
            summary.toneClass = 'alert-danger';
            summary.title = '本次 PRD 风险上升或质量下降，建议先处理高风险问题再继续推进。';
        } else if ((qualityDiff !== null && qualityDiff > 0) || assetDiff > 0) {
            summary.toneClass = 'alert-primary';
            summary.title = '本次 PRD 有明显改进，但仍建议继续清理剩余高风险问题。';
        } else if (p0Right && p0Right > 0) {
            summary.toneClass = 'alert-warning';
            summary.title = '两次结果差异有限，但当前仍残留高风险问题，需要继续收敛。';
        }
        return summary;
    }

    function buildHistoryCompareExport(summary, left, right, lp, rp, leftAssetCount, rightAssetCount) {
        var lines = [];
        lines.push('# 历史快照对比结果');
        lines.push('');
        lines.push('## 对比结论');
        lines.push('- 总结：' + String(summary.title || ''));
        (summary.items || []).forEach(function(item) {
            lines.push('- ' + String(item || ''));
        });
        lines.push('');
        lines.push('## 建议动作');
        (summary.actions || []).forEach(function(action) {
            lines.push('- ' + String(action || ''));
        });
        if (!(summary.actions || []).length) {
            lines.push('- 暂无额外建议动作');
        }
        lines.push('');
        lines.push('## 对比对象');
        lines.push('- 基线版本：' + toHistoryLabel(left));
        lines.push('- 基线快照ID：' + String(left.snapshot_id || ''));
        lines.push('- 对比版本：' + toHistoryLabel(right));
        lines.push('- 对比快照ID：' + String(right.snapshot_id || ''));
        lines.push('');
        lines.push('## 关键指标');
        lines.push('| 指标 | 基线版本 | 对比版本 |');
        lines.push('| :--- | :--- | :--- |');
        lines.push('| 核心问题 | ' + String(lp.main_problem || '—') + ' | ' + String(rp.main_problem || '—') + ' |');
        lines.push('| 重点模块 | ' + String((lp.top_modules || []).join('、') || '—') + ' | ' + String((rp.top_modules || []).join('、') || '—') + ' |');
        lines.push('| 模式/分轨 | ' + String((left.offline_mode ? '本地模式' : 'AI模式') + ' / ' + (left.lane || lp.lane || 'none')) + ' | ' + String((right.offline_mode ? '本地模式' : 'AI模式') + ' / ' + (right.lane || rp.lane || 'none')) + ' |');
        lines.push('| 质量分/等级 | ' + String(lp.quality_score || '—') + ' / ' + String(lp.quality_grade || '—') + ' | ' + String(rp.quality_score || '—') + ' / ' + String(rp.quality_grade || '—') + ' |');
        lines.push('| 风险分布 | P0 ' + String(left.p0_count || 0) + ' / P1 ' + String(left.p1_count || 0) + ' / P2 ' + String(left.p2_count || 0) + ' | P0 ' + String(right.p0_count || 0) + ' / P1 ' + String(right.p1_count || 0) + ' / P2 ' + String(right.p2_count || 0) + ' |');
        lines.push('| 总缺陷数 | ' + String(left.defects_count || 0) + ' | ' + String(right.defects_count || 0) + ' |');
        lines.push('| 命中来源 | 规则 ' + String(left.source_rule_count || 0) + ' / LLM ' + String(left.source_llm_count || 0) + ' | 规则 ' + String(right.source_rule_count || 0) + ' / LLM ' + String(right.source_llm_count || 0) + ' |');
        lines.push('| 认知大纲 | ' + (lp.has_llm_outline ? '有' : '无') + ' | ' + (rp.has_llm_outline ? '有' : '无') + ' |');
        lines.push('| 测试矩阵 | ' + (lp.has_test_matrix ? '有' : '无') + ' | ' + (rp.has_test_matrix ? '有' : '无') + ' |');
        lines.push('| 知识图谱 | ' + (lp.has_kg ? '有' : '无') + ' | ' + (rp.has_kg ? '有' : '无') + ' |');
        lines.push('| 依赖分析 | ' + (lp.has_dependency_analysis ? '有' : '无') + ' | ' + (rp.has_dependency_analysis ? '有' : '无') + ' |');
        lines.push('| 理解卡片 | ' + (lp.has_understanding_cards ? ('有（' + String(lp.understanding_card_count || 0) + '）') : '无') + ' | ' + (rp.has_understanding_cards ? ('有（' + String(rp.understanding_card_count || 0) + '）') : '无') + ' |');
        lines.push('| 资产完整度 | ' + String(leftAssetCount) + ' | ' + String(rightAssetCount) + ' |');
        return {
            filename: 'history_compare_' + safeHistoryFilePart(lp.product_name || left.snapshot_id) + '_vs_' + safeHistoryFilePart(rp.product_name || right.snapshot_id) + '.md',
            text: lines.join('\n')
        };
    }

    function exportHistoryCompareResult() {
        if (!latestHistoryCompareExport || !latestHistoryCompareExport.text) {
            showNonBlockingToast('当前没有可导出的对比结果', 'danger');
            return;
        }
        var blob = new Blob([latestHistoryCompareExport.text], { type: 'text/markdown;charset=utf-8' });
        var url = URL.createObjectURL(blob);
        var link = document.createElement('a');
        link.href = url;
        link.download = latestHistoryCompareExport.filename || 'history_compare.md';
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        URL.revokeObjectURL(url);
        showNonBlockingToast('已导出对比结果', 'success');
    }

    function renderHistoryCompareContent(items) {
        if (!historyCompareContentEl) return;
        if (!Array.isArray(items) || items.length !== 2) {
            historyCompareContentEl.innerHTML = '<div class="text-muted">请选择两条历史记录后进行对比。</div>';
            latestHistoryCompareExport = null;
            if (btnExportHistoryCompare) btnExportHistoryCompare.disabled = true;
            return;
        }
        items.sort(function(a, b) {
            return (a.created_at || 0) - (b.created_at || 0);
        });
        var left = items[0] || {};
        var right = items[1] || {};
        var lp = (left.preview && typeof left.preview === 'object') ? left.preview : {};
        var rp = (right.preview && typeof right.preview === 'object') ? right.preview : {};
        var leftAssetCount = [lp.has_llm_outline, lp.has_test_matrix, lp.has_kg, lp.has_dependency_analysis, lp.has_understanding_cards].filter(Boolean).length;
        var rightAssetCount = [rp.has_llm_outline, rp.has_test_matrix, rp.has_kg, rp.has_dependency_analysis, rp.has_understanding_cards].filter(Boolean).length;
        var summary = buildHistoryCompareSummary(left, right, lp, rp, leftAssetCount, rightAssetCount);
        latestHistoryCompareExport = buildHistoryCompareExport(summary, left, right, lp, rp, leftAssetCount, rightAssetCount);
        if (btnExportHistoryCompare) btnExportHistoryCompare.disabled = false;
        var html = '';
        html += '<div class="alert ' + summary.toneClass + ' border mb-3">';
        html += '<div class="fw-semibold mb-2">' + escapeHtml(summary.title || '') + '</div>';
        html += '<ul class="mb-0 ps-3">';
        (summary.items || []).forEach(function(item) {
            html += '<li>' + escapeHtml(String(item || '')) + '</li>';
        });
        html += '</ul>';
        if ((summary.actions || []).length) {
            html += '<div class="mt-3 pt-3 border-top">';
            html += '<div class="fw-semibold mb-2">建议动作</div>';
            html += '<ul class="mb-0 ps-3">';
            (summary.actions || []).forEach(function(action) {
                html += '<li>' + escapeHtml(String(action || '')) + '</li>';
            });
            html += '</ul>';
            html += '</div>';
        }
        html += '</div>';
        html += '<div class="row g-3 mb-3">';
        html += '<div class="col-12 col-md-4"><div class="border rounded p-3 h-100"><div class="small text-muted">质量变化</div><div class="fs-5 fw-semibold">' + historyValueText(lp.quality_score, '—') + ' → ' + historyValueText(rp.quality_score, '—') + ' ' + historyDeltaBadge(lp.quality_score, rp.quality_score, false) + '</div><div class="small text-muted mt-2">等级：' + historyValueText(lp.quality_grade, '—') + ' → ' + historyValueText(rp.quality_grade, '—') + '</div></div></div>';
        html += '<div class="col-12 col-md-4"><div class="border rounded p-3 h-100"><div class="small text-muted">P0 风险变化</div><div class="fs-5 fw-semibold">' + historyValueText(left.p0_count, '0') + ' → ' + historyValueText(right.p0_count, '0') + ' ' + historyDeltaBadge(left.p0_count, right.p0_count, true) + '</div><div class="small text-muted mt-2">总缺陷：' + historyValueText(left.defects_count, '0') + ' → ' + historyValueText(right.defects_count, '0') + '</div></div></div>';
        html += '<div class="col-12 col-md-4"><div class="border rounded p-3 h-100"><div class="small text-muted">资产完整度</div><div class="fs-5 fw-semibold">' + leftAssetCount + ' → ' + rightAssetCount + ' ' + historyDeltaBadge(leftAssetCount, rightAssetCount, false) + '</div><div class="small text-muted mt-2">看认知大纲 / 测试矩阵 / KG / 依赖分析 / 理解卡片</div></div></div>';
        html += '</div>';

        html += '<div class="table-responsive">';
        html += '<table class="table table-bordered align-middle">';
        html += '<thead><tr><th style="width:220px;">对比项</th><th style="width:calc(40% - 110px);">基线版本</th><th style="width:calc(40% - 110px);">对比版本</th><th style="width:120px;">变化</th></tr></thead><tbody>';
        html += '<tr><th>记录</th><td><div class="fw-semibold">' + escapeHtml(toHistoryLabel(left)) + '</div><div class="small text-muted">' + escapeHtml(String(left.snapshot_id || '')) + '</div></td><td><div class="fw-semibold">' + escapeHtml(toHistoryLabel(right)) + '</div><div class="small text-muted">' + escapeHtml(String(right.snapshot_id || '')) + '</div></td><td class="text-muted small">按时间先后排序</td></tr>';
        html += '<tr><th>核心问题</th><td>' + historyValueText(lp.main_problem, '—') + '</td><td>' + historyValueText(rp.main_problem, '—') + '</td><td class="text-muted small">看问题是否收敛</td></tr>';
        html += '<tr><th>重点模块</th><td>' + historyValueText((lp.top_modules || []).join('、'), '—') + '</td><td>' + historyValueText((rp.top_modules || []).join('、'), '—') + '</td><td class="text-muted small">看问题集中区域</td></tr>';
        html += '<tr><th>模式 / 分轨</th><td>' + (left.offline_mode ? '本地模式' : 'AI模式') + ' / ' + historyValueText(left.lane || lp.lane, 'none') + '</td><td>' + (right.offline_mode ? '本地模式' : 'AI模式') + ' / ' + historyValueText(right.lane || rp.lane, 'none') + '</td><td class="text-muted small">看扫描能力来源</td></tr>';
        html += '<tr><th>质量分 / 等级</th><td>' + historyValueText(lp.quality_score, '—') + ' / ' + historyValueText(lp.quality_grade, '—') + '</td><td>' + historyValueText(rp.quality_score, '—') + ' / ' + historyValueText(rp.quality_grade, '—') + '</td><td>' + historyDeltaBadge(lp.quality_score, rp.quality_score, false) + '</td></tr>';
        html += '<tr><th>风险分布</th><td>P0 ' + historyValueText(left.p0_count, '0') + ' / P1 ' + historyValueText(left.p1_count, '0') + ' / P2 ' + historyValueText(left.p2_count, '0') + '</td><td>P0 ' + historyValueText(right.p0_count, '0') + ' / P1 ' + historyValueText(right.p1_count, '0') + ' / P2 ' + historyValueText(right.p2_count, '0') + '</td><td>' + historyDeltaBadge(left.p0_count, right.p0_count, true) + '</td></tr>';
        html += '<tr><th>命中来源</th><td>规则 ' + historyValueText(left.source_rule_count, '0') + ' / LLM ' + historyValueText(left.source_llm_count, '0') + '</td><td>规则 ' + historyValueText(right.source_rule_count, '0') + ' / LLM ' + historyValueText(right.source_llm_count, '0') + '</td><td class="text-muted small">看审计来源占比</td></tr>';
        html += '<tr><th>认知大纲（LLM）</th><td>' + historyBoolBadge(lp.has_llm_outline) + '</td><td>' + historyBoolBadge(rp.has_llm_outline) + '</td><td class="text-muted small">看是否沉淀认知层</td></tr>';
        html += '<tr><th>测试矩阵</th><td>' + historyBoolBadge(lp.has_test_matrix) + '</td><td>' + historyBoolBadge(rp.has_test_matrix) + '</td><td class="text-muted small">看是否形成验证视图</td></tr>';
        html += '<tr><th>知识图谱</th><td>' + historyBoolBadge(lp.has_kg) + '</td><td>' + historyBoolBadge(rp.has_kg) + '</td><td class="text-muted small">看结构关系是否生成</td></tr>';
        html += '<tr><th>依赖分析</th><td>' + historyBoolBadge(lp.has_dependency_analysis) + '</td><td>' + historyBoolBadge(rp.has_dependency_analysis) + '</td><td class="text-muted small">看系统影响分析是否具备</td></tr>';
        html += '<tr><th>理解卡片</th><td>' + historyBoolBadge(lp.has_understanding_cards) + ' <span class="text-muted">(' + historyValueText(lp.understanding_card_count, '0') + ')</span></td><td>' + historyBoolBadge(rp.has_understanding_cards) + ' <span class="text-muted">(' + historyValueText(rp.understanding_card_count, '0') + ')</span></td><td>' + historyDeltaBadge(lp.understanding_card_count, rp.understanding_card_count, false) + '</td></tr>';
        html += '</tbody></table>';
        html += '</div>';
        historyCompareContentEl.innerHTML = html;
    }

    function compareSelectedSnapshots() {
        if (historyCompareSelection.length !== 2) {
            showNonBlockingToast('请选择两条历史记录后再对比', 'danger');
            return;
        }
        if (!historyCompareContentEl) return;
        historyCompareContentEl.innerHTML = '<div class="p-4 text-center text-muted"><div class="spinner-border spinner-border-sm me-2" role="status"></div>正在生成对比...</div>';
        Promise.all(historyCompareSelection.map(function(sid) {
            return apiJson(baseUrl + '/api/history/snapshot/' + encodeURIComponent(String(sid)));
        }))
        .then(function(items) {
            renderHistoryCompareContent(items || []);
            if (historyCompareModal) historyCompareModal.show();
        })
        .catch(function(err) {
            historyCompareContentEl.innerHTML = '<div class="text-danger">生成对比失败：' + escapeHtml(err.message || String(err)) + '</div>';
            if (historyCompareModal) historyCompareModal.show();
        });
    }

    if (btnCompareSnapshots) {
        btnCompareSnapshots.addEventListener('click', function() {
            compareSelectedSnapshots();
        });
    }
    if (btnExportHistoryCompare) {
        btnExportHistoryCompare.addEventListener('click', function() {
            exportHistoryCompareResult();
        });
    }
    updateHistoryCompareUi();

    if (historyModalEl) {
        historyModalEl.addEventListener('show.bs.modal', function () {
            var listEl = document.getElementById('historySnapshotsList');
            historyCompareSelection = [];
            updateHistoryCompareUi();
            listEl.innerHTML = '<div class="p-4 text-center text-muted small"><div class="spinner-border spinner-border-sm me-2" role="status"></div>正在加载历史记录...</div>';
            
            fetch(baseUrl + '/api/history/snapshots')
                .then(r => r.json())
                .then(data => {
                    if (!data.success) throw new Error(data.message || '加载失败');
                    renderHistorySnapshotsList(data.data.snapshots || []);
                })
                .catch(err => {
                    listEl.innerHTML = '<div class="p-4 text-center text-danger small">加载失败: ' + escapeHtml(err.message) + '</div>';
                });
        });
    }

    function loadSnapshotDetail(snapshotId) {
        var modal = bootstrap.Modal.getInstance(document.getElementById('historySnapshotsModal'));
        if (modal) modal.hide();
        
        statusText.textContent = '正在加载历史记录...';
        btnGenerate.disabled = true;
        
        fetch(baseUrl + '/api/history/snapshot/' + snapshotId)
            .then(r => r.json())
            .then(data => {
                btnGenerate.disabled = false;
                if (!data.success) throw new Error(data.message || '加载失败');
                
                var payload = data.data || {};
                var preview = (payload.preview && typeof payload.preview === 'object') ? payload.preview : {};
                
                // 恢复输入框
                if (inputEl && payload.prd_text) {
                    inputEl.value = payload.prd_text;
                }
                
                // 恢复报告
                if (payload.reports) {
                    reports.L1 = payload.reports.L1 || '';
                    reports.L2 = payload.reports.L2 || '';
                    reports.L3 = payload.reports.L3 || '';
                }
                
                // 恢复额外数据结构
                var stage3 = payload.reports || {};
                var extras = payload.extras || {};
                
                reports.architecture_scan = extras.architecture_scan || null;
                reports.validation_outline = extras.validation_outline || null;
                
                // 尝试重构渲染所需的对象
                try {
                    testMatrix = extras.test_matrix || null;
                    diagrams = extras.diagrams || null;
                    kg = extras.kg || null;
                    outlineEngine = extras.outline_engine || null;
                    platformImpact = extras.platform_impact || null;
                    dependencyAnalysis = extras.dependency_analysis || null;
                    prdQuality = extras.prd_quality || null;
                    testPoints = extras.test_points || null;
                    validationOutline = extras.validation_outline || null;  // 新增：验证大纲
                    riskPrediction = extras.risk_prediction || null;
                    understandingCards = extras.understanding_cards || null;
                    releaseGate = extras.release_gate || null;
                    sharedSummary = extras.shared_summary || null;
                    readerGuide = extras.reader_guide || null;
                    
                    // 恢复 LLM 四支柱大纲
                    if (extras.outline_llm && extras.outline_llm.ok) {
                        llmFourPillarsPayload = { llm: extras.outline_llm, stage1_output: payload.stage1_output };
                    } else {
                        llmFourPillarsPayload = null;
                    }
                    
                    // 如果是 L3 报告，通常包含了这些结构化数据，如果 payload 中有也可以恢复
                    var stage2 = payload.stage2_output || {};
                    defectsData = stage2.defects || [];
                    scanMetaData = (stage2.scan_meta && typeof stage2.scan_meta === 'object') ? stage2.scan_meta : null;
                    summaryData = buildMinimalSummaryFromDefects(defectsData);
                    reports.OUTLINE = buildOutlineReportMarkdown();
                    
                    // 重新渲染各个组件
                    renderExtras();
                    setLevel(reportLevel);
                    var loadedMsg = '已加载历史审计报告';
                    if (preview.product_name) loadedMsg += '：' + String(preview.product_name);
                    showNonBlockingToast(loadedMsg, 'success');
                    
                    if (testMatrix) {
                        saveMatrixSnapshot();
                    }
                } catch (e) {
                    console.error("恢复结构化数据失败", e);
                }
                
                statusText.textContent = preview.product_name ? ('历史记录加载完成：' + preview.product_name) : '历史记录加载完成';
            })
            .catch(err => {
                btnGenerate.disabled = false;
                statusText.textContent = '加载失败';
                alert('加载历史记录失败: ' + err.message);
            });
    }

    function apiJson(url, options) {
        return fetch(url, options || {})
            .then(function(r) { return r.json().then(function(d){ return { ok: r.ok, body: d }; }); })
            .then(function(resp) {
                if (!resp.ok || !resp.body || !resp.body.success) {
                    throw new Error((resp.body && resp.body.message) || '请求失败');
                }
                return resp.body.data || {};
            });
    }

    function showNonBlockingToast(message, kind) {
        var msg = String(message || '').trim();
        if (!msg) return;
        if (!globalToastEl || !globalToastBodyEl || !globalToast) {
            return;
        }
        globalToastBodyEl.textContent = msg;
        var k = String(kind || 'success');
        globalToastEl.className = 'toast align-items-center border-0 text-bg-' + (k === 'danger' ? 'danger' : 'success');
        globalToast.show();
    }

    function baseName(p) {
        var s = String(p || '');
        var i = Math.max(s.lastIndexOf('/'), s.lastIndexOf('\\'));
        return i >= 0 ? s.slice(i + 1) : s;
    }

    function cssEscapeText(s) {
        var t = String(s || '');
        if (window.CSS && typeof window.CSS.escape === 'function') return window.CSS.escape(t);
        return t.replace(/["\\#.;:[\],=]/g, '\\$&');
    }

    function setBugStatus(text, isError) {
        if (!bugStatusEl) return;
        bugStatusEl.textContent = String(text || '');
        bugStatusEl.className = 'small ' + (isError ? 'text-danger mb-2' : 'text-muted mb-2');
    }

    function renderBugResult(title, payload) {
        if (!bugResultEl) return;
        var html = '<div class="fw-semibold small mb-1">' + escapeHtml(title || '结果') + '</div>';
        if (!payload || typeof payload !== 'object') {
            bugResultEl.innerHTML = html + '<div class="text-muted small">无结果</div>';
            return;
        }
        if (Array.isArray(payload.items) && payload.items.length) {
            html += '<div class="table-responsive"><table class="table table-sm table-bordered extra-table mb-2">';
            html += '<thead><tr><th style="width:130px;">类型</th><th>内容</th></tr></thead><tbody>';
            payload.items.slice(0, 20).forEach(function(it) {
                var left = escapeHtml(String(it.pattern_id || it.category || it.id || '-'));
                var right = escapeHtml(String(it.bug_desc || it.rule || it.content || JSON.stringify(it)));
                html += '<tr><td>' + left + '</td><td>' + right + '</td></tr>';
            });
            html += '</tbody></table></div>';
        }
        if (Array.isArray(payload.bug_hits) && payload.bug_hits.length) {
            html += '<div class="small mb-1">Bug命中：' + escapeHtml(String(payload.bug_hits.length)) + '</div>';
        }
        if (Array.isArray(payload.vector_hits) && payload.vector_hits.length) {
            html += '<div class="small text-muted">向量命中：' + escapeHtml(String(payload.vector_hits.length)) + '</div>';
        }
        bugResultEl.innerHTML = html;
    }

    // 知识库匹配结果展示（本地规则匹配）
    function renderKnowledgeMatches(matches) {
        var container = document.getElementById('knowledgeMatchesContainer');
        if (!container) {
            // 创建容器
            var bugResultEl = document.getElementById('bugResult');
            if (!bugResultEl) return;
            container = document.createElement('div');
            container.id = 'knowledgeMatchesContainer';
            container.className = 'mt-3 p-3 bg-light rounded';
            container.innerHTML = '<div class="fw-semibold small mb-2"><i class="fas fa-lightbulb text-warning me-1"></i>知识库匹配建议（本地规则匹配）</div><div id="knowledgeMatchesList"></div>';
            bugResultEl.parentNode.insertBefore(container, bugResultEl.nextSibling);
        }
        
        var listEl = document.getElementById('knowledgeMatchesList');
        if (!listEl) return;
        
        if (!matches || matches.length === 0) {
            listEl.innerHTML = '<div class="text-muted small">未匹配到相关知识库条目</div>';
            return;
        }
        
        var html = '<div class="table-responsive"><table class="table table-sm table-bordered mb-0">';
        html += '<thead><tr><th style="width:80px;">优先级</th><th style="width:120px;">名称</th><th>领域</th><th>触发条件</th><th>建议</th></tr></thead><tbody>';
        
        matches.forEach(function(it) {
            var priorityClass = {'P0': 'danger', 'P1': 'warning', 'P2': 'info', 'P3': 'secondary'}[it.priority] || 'secondary';
            html += '<tr>';
            html += '<td><span class="badge bg-' + priorityClass + '">' + escapeHtml(it.priority || '-') + '</span></td>';
            html += '<td>' + escapeHtml(it.name || '-') + '</td>';
            html += '<td>' + escapeHtml(it.domain || '-') + '</td>';
            html += '<td>' + escapeHtml(it.trigger || '-') + '</td>';
            html += '<td>' + escapeHtml(it.suggestion || '-') + '</td>';
            html += '</tr>';
        });
        
        html += '</tbody></table></div>';
        html += '<div class="small text-muted mt-1">* 基于PRD关键词自动匹配知识库，不需要大模型</div>';
        
        listEl.innerHTML = html;
    }

    function escapeHtml(s) {
        return String(s == null ? '' : s)
          .replace(/&/g, '&amp;')
          .replace(/</g, '&lt;')
          .replace(/>/g, '&gt;')
          .replace(/\"/g, '&quot;')
          .replace(/'/g, '&#39;');
    }

    function collectOwnerCorrectionRows() {
        var tbody = document.getElementById('explicitOwnerTableBody');
        if (!tbody) return [];
        var rows = [];
        tbody.querySelectorAll('tr').forEach(function(tr) {
            var step = String(tr.getAttribute('data-step') || '').trim();
            var ownerEl = tr.querySelector('.owner-input');
            var actionEl = tr.querySelector('.action-cell');
            var inputEl2 = tr.querySelector('.input-cell');
            var outputEl2 = tr.querySelector('.output-cell');
            var owner = String((ownerEl && ownerEl.value) || '').trim();
            var action = String((actionEl && actionEl.getAttribute('data-action')) || (actionEl && actionEl.textContent) || '').trim();
            var inHint = String((inputEl2 && inputEl2.getAttribute('data-input')) || (inputEl2 && inputEl2.textContent) || '').trim();
            var outHint = String((outputEl2 && outputEl2.getAttribute('data-output')) || (outputEl2 && outputEl2.textContent) || '').trim();
            if (!step && !action) return;
            rows.push({ step: step, owner: owner, action: action, input: inHint, output: outHint });
        });
        return rows;
    }

    function bindOwnerCorrectionActions() {
        var btn = document.getElementById('btnSaveOwnerCorrection');
        var statusEl = document.getElementById('ownerCorrectionStatus');
        if (!btn) return;
        btn.onclick = function() {
            var flowRows = collectOwnerCorrectionRows();
            if (!flowRows.length) {
                if (statusEl) statusEl.textContent = '无可提交的校正数据';
                return;
            }
            if (statusEl) statusEl.textContent = '保存中...';
            var payload = {
                prd_text: String((inputEl && inputEl.value) || ''),
                flow_rows: flowRows,
                role_rows: Array.isArray(explicitRoleRowsLatest) ? explicitRoleRowsLatest : [],
                meta: {
                    source: 'outline_manual_owner_fix',
                    report_level: reportLevel,
                    coverage: (explicitOutlineLatest && explicitOutlineLatest.coverage) || {},
                    readability: (explicitOutlineLatest && explicitOutlineLatest.quality_signals && explicitOutlineLatest.quality_signals.readability_score) || 0
                }
            };
            apiJson('/prd_audit/api/learning/outline_owner_correction', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            }).then(function(res) {
                if (!res || !res.success) throw new Error((res && res.message) || '保存失败');
                if (statusEl) statusEl.textContent = '已保存：' + String((res.data && res.data.correction_id) || '');
                showGlobalToast('责任角色校正已保存到学习快照', 'success');
            }).catch(function(err) {
                if (statusEl) statusEl.textContent = '保存失败：' + String(err.message || err);
                showGlobalToast('责任角色校正保存失败', 'danger');
            });
        };
    }

    function renderKeyValueTable(title, rows) {
        if (!rows || !rows.length) return '';
        var html = '';
        if (title) html += '<div class="fw-semibold mb-2">' + escapeHtml(title) + '</div>';
        html += '<div class="table-responsive"><table class="table table-sm table-striped extra-table mb-3">';
        html += '<thead><tr><th style="width:180px;">项</th><th>内容</th></tr></thead><tbody>';
        rows.forEach(function(r) {
            html += '<tr><td>' + escapeHtml(r.k) + '</td><td>' + escapeHtml(r.v) + '</td></tr>';
        });
        html += '</tbody></table></div>';
        return html;
    }

    function renderBoundaryMatrix(items) {
        if (!items || !items.length) return '';
        var html = '<div class="fw-semibold mb-2">边界/异常覆盖速览</div>';
        html += '<div class="table-responsive"><table class="table table-sm table-striped extra-table mb-3">';
        html += '<thead><tr><th>检查项</th><th>是否覆盖</th><th>建议</th></tr></thead><tbody>';
        items.forEach(function(it) {
            html += '<tr><td>' + escapeHtml(it.item) + '</td><td>' + (it.covered ? '<span class="badge bg-success">覆盖</span>' : '<span class="badge bg-warning text-dark">缺失</span>') + '</td><td>' + escapeHtml(it.suggestion || '') + '</td></tr>';
        });
        html += '</tbody></table></div>';
        return html;
    }

    function renderFunctionMatrix(items) {
        if (!items || !items.length) return '';
        var cols = ['正常流程','异常流程','边界条件','并发场景','中断恢复'];
        var html = '<div class="fw-semibold mb-2">功能测试矩阵 <small class="text-muted fw-normal ms-2">（<i class="fas fa-check-circle text-success"></i> 覆盖 / <i class="fas fa-times-circle text-danger"></i> 缺失 / <i class="fas fa-question-circle text-warning"></i> 待定）</small></div>';
        html += '<div class="table-responsive"><table class="table table-sm table-bordered extra-table matrix-table mb-3">';
        html += '<thead><tr><th style="min-width:140px;">模块</th>' + cols.map(function(c){return '<th class="text-center">' + escapeHtml(c) + '</th>';}).join('') + '</tr></thead><tbody>';
        items.forEach(function(row) {
            var moduleName = String(row.module || '').replace(/[\x00-\x1f\x7f-\x9f]/g, ' ').replace(/\s+/g, ' ').trim();
            html += '<tr><td><div class="matrix-module" title="' + escapeHtml(moduleName || '-') + '">' + escapeHtml(moduleName || '-') + '</div></td>';
            cols.forEach(function(c) {
                var cell = (row.test_types || {})[c] || {};
                var status = String(cell.status || '');
                var risk = String(cell.risk_level || '');
                var caseId = String(cell.case_id || '');
                var expected = String(cell.expected || '');
                var evidence = String(cell.evidence || '');
                var suggestion = String(cell.suggestion || '');
                
                var icon = '<i class="fas fa-minus text-muted" style="opacity:0.3"></i>';
                if (status === '缺失') icon = '<i class="fas fa-times-circle text-danger matrix-status-icon"></i>';
                else if (status === '覆盖') icon = '<i class="fas fa-check-circle text-success matrix-status-icon"></i>';
                else if (status === '待确认') icon = '<i class="fas fa-question-circle text-warning matrix-status-icon"></i>';
                
                var riskBadge = '';
                if (risk) {
                    var riskCls = risk === 'P0' ? 'risk-p0' : (risk === 'P1' ? 'risk-p1' : 'risk-p2');
                    riskBadge = '<span class="matrix-risk-tag ' + riskCls + '">' + escapeHtml(risk) + '</span>';
                }
                
                var btnDetail = '';
                if (expected || evidence || suggestion) {
                    // Store data in attributes (escape properly)
                    var dataAttrs = 'data-case="' + escapeHtml(caseId) + '" ' +
                                    'data-status="' + escapeHtml(status) + '" ' +
                                    'data-risk="' + escapeHtml(risk) + '" ' +
                                    'data-exp="' + escapeHtml(expected) + '" ' +
                                    'data-evi="' + escapeHtml(evidence) + '" ' +
                                    'data-sug="' + escapeHtml(suggestion) + '"';
                    btnDetail = '<button type="button" class="btn-matrix-detail" ' + dataAttrs + '>详情</button>';
                }

                html += '<td class="text-center"><div class="matrix-cell-content">' + 
                        '<div>' + icon + (riskBadge ? ' ' + riskBadge : '') + '</div>' +
                        (caseId ? '<div class="text-muted" style="font-size:0.65rem;">' + escapeHtml(caseId) + '</div>' : '') +
                        btnDetail +
                        '</div></td>';
            });
            html += '</tr>';
        });
        html += '</tbody></table></div>';
        return html;
    }

    function renderPermissionMatrix(items) {
        if (!items || !items.length) return '';
        var html = '<div class="fw-semibold mb-2">权限矩阵（简化）</div>';
        html += '<div class="table-responsive"><table class="table table-sm table-striped extra-table mb-3">';
        html += '<thead><tr><th style="min-width:160px;">角色</th><th>覆盖模块</th><th>备注</th></tr></thead><tbody>';
        items.forEach(function(r) {
            html += '<tr><td>' + escapeHtml(r.role) + '</td><td>' + escapeHtml((r.modules || []).join('、')) + '</td><td>' + escapeHtml(r.note || '') + '</td></tr>';
        });
        html += '</tbody></table></div>';
        return html;
    }

    function renderConcurrentMatrix(obj) {
        if (!obj) return '';
        var keys = Object.keys(obj || {});
        if (!keys.length) return '';
        var html = '<div class="fw-semibold mb-2">并发矩阵（简化）</div>';
        html += '<div class="table-responsive"><table class="table table-sm table-striped extra-table mb-3">';
        html += '<thead><tr><th>并发组合</th><th>状态</th><th>风险</th><th>建议</th></tr></thead><tbody>';
        keys.slice(0, 30).forEach(function(k) {
            var it = obj[k] || {};
            var badge = it.status === '未定义' ? '<span class="badge bg-warning text-dark">未定义</span>' : '<span class="badge bg-success">已定义</span>';
            html += '<tr><td>' + escapeHtml(k) + '</td><td>' + badge + '</td><td>' + escapeHtml(it.risk_level || '') + '</td><td>' + escapeHtml(it.suggestion || '') + '</td></tr>';
        });
        html += '</tbody></table></div>';
        return html;
    }

    function buildMatrixSections(options) {
        var opts = options || {};
        var filterMissing = !!opts.onlyMissing;
        var filterP0 = !!opts.onlyP0;

        function cellMatched(status, risk) {
            var ok = true;
            if (filterMissing) ok = ok && String(status || '') === '缺失';
            if (filterP0) ok = ok && String(risk || '') === 'P0';
            return ok;
        }

        function filterFunctionMatrix(items) {
            if (!Array.isArray(items) || !items.length) return [];
            if (!filterMissing && !filterP0) return items;
            var cols = ['正常流程','异常流程','边界条件','并发场景','中断恢复'];
            return items.filter(function(row) {
                var tt = row && typeof row === 'object' ? (row.test_types || {}) : {};
                return cols.some(function(c) {
                    var cell = tt[c] || {};
                    return cellMatched(cell.status, cell.risk_level);
                });
            });
        }

        function filterBoundaryMatrix(items) {
            if (!Array.isArray(items) || !items.length) return [];
            if (!filterMissing && !filterP0) return items;
            if (filterP0) return [];
            return items.filter(function(it) { return !it.covered; });
        }

        function filterConcurrentMatrix(obj) {
            if (!obj || typeof obj !== 'object') return {};
            if (!filterMissing && !filterP0) return obj;
            var out = {};
            Object.keys(obj).forEach(function(k) {
                var it = obj[k] || {};
                var st = String(it.status || '') === '未定义' ? '缺失' : '覆盖';
                if (cellMatched(st, it.risk_level)) out[k] = it;
            });
            return out;
        }

        function filterPermissionMatrix(items) {
            if (!Array.isArray(items) || !items.length) return [];
            if (!filterMissing && !filterP0) return items;
            return [];
        }

        var sections = [];
        var fx = renderFunctionMatrix(filterFunctionMatrix(testMatrix.function_matrix));
        if (fx) sections.push({ key: 'function', title: '功能测试矩阵', html: fx });
        var bd = renderBoundaryMatrix(filterBoundaryMatrix(testMatrix.boundary_matrix));
        if (bd) sections.push({ key: 'boundary', title: '边界/异常覆盖', html: bd });
        var cc = renderConcurrentMatrix(filterConcurrentMatrix(testMatrix.concurrent_matrix));
        if (cc) sections.push({ key: 'concurrent', title: '并发矩阵', html: cc });
        var pm = renderPermissionMatrix(filterPermissionMatrix(testMatrix.permission_matrix));
        if (pm) sections.push({ key: 'permission', title: '权限矩阵', html: pm });
        return sections;
    }

    function renderMatrixFullView(sections) {
        if (!matrixFullContentEl) return;
        if (!sections || !sections.length) {
            matrixFullContentEl.innerHTML = '<div class="text-muted small">暂无测试矩阵数据</div>';
            return;
        }
        var summary = testMatrix && typeof testMatrix === 'object' ? (testMatrix.summary || {}) : {};
        var cards = [
            { k: '模块总数', v: String(summary.modules_total || 0) },
            { k: '缺失项', v: String(summary.missing_cases || 0) },
            { k: '高风险', v: String(summary.high_risk_cases || 0) },
            { k: '覆盖率', v: String(summary.coverage || '-') }
        ];
        var hasFilter = matrixFilters.missing || matrixFilters.p0;
        var html = '<div class="matrix-full-toolbar">';
        html += '<div class="matrix-summary-cards">' + cards.map(function(c) {
            return '<div class="matrix-summary-card"><div class="k">' + escapeHtml(c.k) + '</div><div class="v">' + escapeHtml(c.v) + '</div></div>';
        }).join('') + '</div>';
        html += '<div class="d-flex flex-wrap gap-2 mb-2">';
        html += '<button type="button" class="btn btn-sm ' + (matrixFilters.missing ? 'btn-primary' : 'btn-outline-primary') + ' btn-matrix-filter" data-filter="missing">只看缺失</button>';
        html += '<button type="button" class="btn btn-sm ' + (matrixFilters.p0 ? 'btn-danger' : 'btn-outline-danger') + ' btn-matrix-filter" data-filter="p0">只看P0</button>';
        if (hasFilter) html += '<span class="small text-muted align-self-center">已启用关键筛选</span>';
        html += '</div>';
        html += '<div class="d-flex flex-wrap gap-2">';
        sections.forEach(function(s) {
            html += '<button type="button" class="btn btn-sm btn-outline-secondary btn-matrix-jump" data-target="matrix_section_' + escapeHtml(s.key) + '">' + escapeHtml(s.title) + '</button>';
        });
        html += '</div></div>';
        if (!sections.length) {
            html += '<div class="text-muted small">当前筛选条件下暂无命中项，请取消筛选查看完整矩阵。</div>';
            matrixFullContentEl.innerHTML = html;
            return;
        }
        sections.forEach(function(s) {
            html += '<div id="matrix_section_' + escapeHtml(s.key) + '" class="matrix-section-anchor mb-3">';
            html += '<div class="fw-semibold mb-2">' + escapeHtml(s.title) + '</div>';
            html += s.html;
            html += '</div>';
        });
        matrixFullContentEl.innerHTML = html;
    }

    function saveMatrixSnapshot() {
        try {
            if (!testMatrix || typeof testMatrix !== 'object') return;
            var snapshot = {
                testMatrix: testMatrix,
                releaseGate: releaseGate || null,
                understandingCards: understandingCards || null,
                saved_at: new Date().toISOString(),
                source: 'prd_audit_index'
            };
            localStorage.setItem(MATRIX_SNAPSHOT_KEY, JSON.stringify(snapshot));
        } catch (_) {}
    }

    function renderStage4() {
        var el = document.getElementById('matrixContent');
        if (!el) return;
        if (!testMatrix || typeof testMatrix !== 'object') {
            el.innerHTML = '<div class="text-muted small">暂无测试矩阵数据</div>';
            return;
        }
        var html = '<div class="small text-muted mb-2">分析矩阵已迁移到独立页面查看。</div>';
        html += '<a class="btn btn-sm btn-dark" href="/prd_audit/matrix_view">进入独立矩阵页</a>';
        el.innerHTML = html;
        saveMatrixSnapshot();
    }

    function renderTestPoints() {
        if (!testPointsEl) return;
        if (!testPoints || typeof testPoints !== 'object') {
            testPointsEl.innerHTML = '<div class="text-muted small">暂无测试点数据</div>';
            return;
        }
        var modules = Array.isArray(testPoints.modules) ? testPoints.modules : [];
        var stats = testPoints.stats || {};
        var html = '';
        html += '<div class="d-flex align-items-center gap-2 mb-2"><button type="button" class="btn btn-sm btn-outline-secondary btn-clear-linkage">清除联动高亮</button><span class="small text-muted linkage-status">' + (activeLinkedTestPointId ? ('当前联动测试点：' + escapeHtml(activeLinkedTestPointId)) : '当前未联动') + '</span></div>';
        html += '<div class="small text-muted mb-2">' + escapeHtml(testPoints.summary || '暂无结论') + '</div>';
        html += '<div class="d-flex flex-wrap gap-2 mb-2">' +
            '<span class="badge bg-primary">模块数 ' + escapeHtml(String(stats.module_count || 0)) + '</span>' +
            '<span class="badge bg-secondary">测试点数 ' + escapeHtml(String(stats.point_count || 0)) + '</span>' +
            '</div>';
        if (!modules.length) {
            testPointsEl.innerHTML = html + '<div class="text-muted small">未识别到可生成测试点的模块。</div>';
            return;
        }
        modules.forEach(function(m) {
            var points = Array.isArray(m.points) ? m.points : [];
            html += '<div class="fw-semibold small mt-2 mb-1">' + escapeHtml(m.module || '-') + '</div>';
            html += '<div class="table-responsive mb-2"><table class="table table-sm table-bordered extra-table mb-0">';
            html += '<thead><tr><th style="width:110px;">ID</th><th>测试点</th><th style="width:90px;">类型</th><th style="width:70px;">优先级</th><th>依据</th></tr></thead><tbody>';
            points.forEach(function(p) {
                var pid = String(p.id || '');
                html += '<tr class="tp-row" data-tp-id="' + escapeHtml(pid) + '"><td><button type="button" class="btn btn-link p-0 align-baseline tp-id-link" data-tpid="' + escapeHtml(pid) + '">' + escapeHtml(pid || '-') + '</button></td><td>' + escapeHtml(p.title || '-') + '</td><td>' + escapeHtml(p.type || '-') + '</td><td>' + escapeHtml(p.priority || '-') + '</td><td>' + escapeHtml(p.evidence || '-') + '</td></tr>';
            });
            html += '</tbody></table></div>';
        });
        testPointsEl.innerHTML = html;
        applyRiskTestPointHighlight();
        
        // 同时渲染验证大纲
        renderValidationOutline();
    }

    // 验证大纲渲染函数
    function renderValidationOutline() {
        var el = document.getElementById('validationOutlineMainContent');
        if (!el) return;
        if (!validationOutline || typeof validationOutline !== 'object') {
            el.innerHTML = '<div class="text-muted small">暂无验证大纲数据</div>';
            return;
        }
        
        var items = Array.isArray(validationOutline.outline_items) ? validationOutline.outline_items : [];
        var stats = validationOutline.stats || {};
        
        var html = '';
        
        // 统计卡片
        html += '<div class="row g-2 mb-3">';
        html += '<div class="col-6 col-md-3"><div class="card bg-light"><div class="card-body p-2 text-center"><div class="h5 mb-0">' + (stats.total_modules || 0) + '</div><div class="small text-muted">功能模块</div></div></div></div>';
        html += '<div class="col-6 col-md-3"><div class="card bg-danger text-white"><div class="card-body p-2 text-center"><div class="h5 mb-0">' + (stats.high_risk_modules || 0) + '</div><div class="small">高风险</div></div></div></div>';
        html += '<div class="col-6 col-md-3"><div class="card bg-warning"><div class="card-body p-2 text-center"><div class="h5 mb-0">' + (stats.auto_smoke || 0) + '</div><div class="small text-muted">冒烟测试</div></div></div></div>';
        html += '<div class="col-6 col-md-3"><div class="card bg-info text-white"><div class="card-body p-2 text-center"><div class="h5 mb-0">' + (stats.auto_regression || 0) + '</div><div class="small">回归测试</div></div></div></div>';
        html += '</div>';
        
        // 摘要
        html += '<div class="alert alert-info small mb-3">' + escapeHtml(validationOutline.summary || '') + '</div>';
        
        if (!items.length) {
            el.innerHTML = html + '<div class="text-muted small">未识别到可生成验证大纲的模块。</div>';
            return;
        }
        
        // 功能验证矩阵表格
        html += '<div class="table-responsive"><table class="table table-sm table-bordered extra-table">';
        html += '<thead><tr><th style="width:120px;">功能模块</th><th style="width:60px;">优先级</th><th style="width:50px;">功能</th><th style="width:50px;">异常</th><th style="width:50px;">并发</th><th style="width:50px;">边界</th><th style="width:50px;">性能</th><th style="width:80px;">自动化</th><th>分层验证建议</th></tr></thead><tbody>';
        
        items.forEach(function(item) {
            var priorityClass = {'P0': 'bg-danger', 'P1': 'bg-warning', 'P2': 'bg-info', 'P3': 'bg-secondary'}[item.priority] || 'bg-secondary';
            var dims = item.validation_matrix || {};
            var autoClass = {'smoke': 'bg-success', 'regression': 'bg-warning', 'full': 'bg-info'}[item.automation_level] || 'bg-secondary';
            var autoText = {'smoke': '冒烟', 'regression': '回归', 'full': '全量'}[item.automation_level] || '-';
            
            // 分层验证建议
            var strategy = item.layer_strategy || {};
            var strategyText = [];
            if (strategy.unit && strategy.unit.length) {
                strategyText.push('<span class="badge bg-primary me-1">单元</span>' + strategy.unit.join('、'));
            }
            if (strategy.integration && strategy.integration.length) {
                strategyText.push('<span class="badge bg-success me-1">集成</span>' + strategy.integration.join('、'));
            }
            if (strategy.e2e && strategy.e2e.length) {
                strategyText.push('<span class="badge bg-info me-1">E2E</span>' + strategy.e2e.join('、'));
            }
            
            html += '<tr>';
            html += '<td><strong>' + escapeHtml(item.module || '-') + '</strong><br><small class="text-muted">' + (item.test_point_count || 0) + '个测试点</small></td>';
            html += '<td><span class="badge ' + priorityClass + '">' + escapeHtml(item.priority || '-') + '</span></td>';
            html += '<td class="text-center">' + (dims.functional ? '<i class="fas fa-check text-success"></i>' : '-') + '</td>';
            html += '<td class="text-center">' + (dims.abnormal ? '<i class="fas fa-check text-success"></i>' : '-') + '</td>';
            html += '<td class="text-center">' + (dims.concurrency ? '<i class="fas fa-check text-success"></i>' : '-') + '</td>';
            html += '<td class="text-center">' + (dims.boundary ? '<i class="fas fa-check text-success"></i>' : '-') + '</td>';
            html += '<td class="text-center">' + (dims.performance ? '<i class="fas fa-check text-success"></i>' : '-') + '</td>';
            html += '<td><span class="badge ' + autoClass + '">' + autoText + '</span></td>';
            html += '<td class="small">' + (strategyText.length ? strategyText.join('<br>') : '-') + '</td>';
            html += '</tr>';
        });
        
        html += '</tbody></table></div>';
        html += '<div class="small text-muted mt-2"><i class="fas fa-info-circle me-1"></i>验证大纲基于测试点自动生成，帮助测试工程师设计分层验证策略</div>';
        
        el.innerHTML = html;
        
        // 如果外层容器存在，确保其显示状态正确
        var wrapEl = document.getElementById('validationOutlineMainWrap');
        if (wrapEl && reportLevel === 'VALIDATION') {
            wrapEl.style.display = 'block';
        }
        
        // 当切换到验证大纲时，强制隐藏四支柱，避免干扰
        var fourPillarsWrap = document.getElementById('fourPillarsAuditWrap');
        if (fourPillarsWrap && reportLevel === 'VALIDATION') {
            fourPillarsWrap.style.display = 'none';
        }
    }

    function normalizeMermaid(text) {
        var s = String(text || '');
        s = s.replace(/```mermaid/g, '').replace(/```/g, '');
        return s.trim();
    }

    function renderMermaidBlock(code, containerId) {
        var el = document.getElementById(containerId);
        if (!el) return;
        var src = normalizeMermaid(code);
        if (!src) {
            el.innerHTML = '<div class="text-muted small">暂无</div>';
            return;
        }
        // 若 mermaid 不可用，直接显示源码
        if (!window.mermaid || typeof window.mermaid.initialize !== 'function') {
            el.innerHTML = '<pre class="extra-pre mb-0">' + escapeHtml(src) + '</pre>';
            return;
        }
        el.innerHTML = '<div class="mermaid">' + escapeHtml(src) + '</div>';
        try {
            window.mermaid.initialize({ startOnLoad: false, securityLevel: 'loose' });
            window.mermaid.init(undefined, el.querySelectorAll('.mermaid'));
        } catch (e) {
            el.innerHTML = '<pre class="extra-pre mb-0">' + escapeHtml(src) + '</pre>';
        }
    }

    function renderStage5() {
        var el = document.getElementById('diagramContent');
        if (!el) return;
        if (!diagrams || typeof diagrams !== 'object') {
            el.innerHTML = '<div class="text-muted small">暂无系统图数据</div>';
            return;
        }
        var html = '';
        html += '<div class="fw-semibold mb-2 small">状态机图</div><div id="diagram_state" class="mb-3"></div>';
        html += '<div class="fw-semibold mb-2 small">并发冲突图</div><div id="diagram_concurrency" class="mb-3"></div>';
        el.innerHTML = html;
        renderMermaidBlock(diagrams.state_diagram || '', 'diagram_state');
        renderMermaidBlock(diagrams.concurrency_diagram || '', 'diagram_concurrency');
    }

    function renderKG() {
        var el = document.getElementById('kgContent');
        if (!el) return;
        if (!kg || typeof kg !== 'object') {
            el.innerHTML = '<div class="text-muted small">暂无根因推理数据</div>';
            return;
        }
        var roots = Array.isArray(kg.root_causes) ? kg.root_causes : [];
        var html = '';

        if (roots.length) {
            html += '<div class="fw-semibold mb-2 small">根因候选</div>';
            html += '<div class="table-responsive"><table class="table table-sm table-striped extra-table mb-3 small">';
            html += '<thead><tr><th>根因ID</th><th>说明</th></tr></thead><tbody>';
            roots.forEach(function(r) {
                html += '<tr><td>' + escapeHtml(r.id) + '</td><td>' + escapeHtml(r.why || '') + '</td></tr>';
            });
            html += '</tbody></table></div>';
        } else {
            html += '<div class="text-muted small mb-3">未识别到明确根因。</div>';
        }
        el.innerHTML = html;
    }

    function cleanMindLabel(text) {
        return String(text || '')
            .replace(/[\r\n\t]/g, ' ')
            .replace(/["`]/g, '')
            .replace(/[<>]/g, '')
            .replace(/\s+/g, ' ')
            .trim();
    }

    function buildFeatureNodes() {
        var rows = [];
        if (outlineEngine && Array.isArray(outlineEngine.nodes) && outlineEngine.nodes.length) {
            rows = outlineEngine.nodes
                .filter(function(n) {
                    var t = cleanMindLabel(n && n.title);
                    var lv = Number((n && n.level) || 0);
                    return t && lv >= 1 && lv <= 4;
                })
                .slice(0, 60)
                .map(function(n) { return { level: Number(n.level), title: cleanMindLabel(n.title) }; });
            if (rows.length) return rows;
        }
        if (parseMeta && Array.isArray(parseMeta.blocks)) {
            rows = parseMeta.blocks
                .filter(function(b) {
                    var t = cleanMindLabel(b && b.title);
                    var lv = Number((b && b.level) || 0);
                    return t && lv >= 1 && lv <= 4;
                })
                .slice(0, 40)
                .map(function(b) { return { level: Number(b.level), title: cleanMindLabel(b.title) }; });
        }

        if (!rows.length) {
            var fallback = (reports.L3 || reports.L2 || reports.L1 || '').split('\n')
                .map(function(line) { return String(line || '').trim(); })
                .filter(function(line) { return /^#{1,4}\s+/.test(line); })
                .slice(0, 30);
            rows = fallback.map(function(line) {
                var m = line.match(/^(#{1,4})\s+(.+)$/);
                return { level: m ? m[1].length : 1, title: cleanMindLabel(m ? m[2] : line) };
            });
        }
        return rows;
    }

    function renderOutlineEngine() {
        if (!outlineEl) return;
        if (!outlineEngine || typeof outlineEngine !== 'object') {
            outlineEl.innerHTML = '<div class="text-muted small">暂无 PRD 理解大纲数据</div>';
            return;
        }
        var qs = outlineEngine.quality_score || {};
        var outline = Array.isArray(outlineEngine.outline) ? outlineEngine.outline : [];
        var html = '';
        html += '<div class="d-flex align-items-center flex-wrap gap-2 mb-2">' +
            '<span class="badge bg-primary">模式: ' + escapeHtml(outlineEngine.mode || 'structured') + '</span>' +
            '<span class="badge bg-secondary">结构完整度 ' + escapeHtml(String(qs.structure_completeness || 0)) + '%</span>' +
            '<span class="badge bg-secondary">模块清晰度 ' + escapeHtml(String(qs.module_clarity || 0)) + '%</span>' +
            '<span class="badge bg-secondary">流程完整度 ' + escapeHtml(String(qs.flow_completeness || 0)) + '%</span>' +
            '<span class="badge ' + ((Number(qs.overall || 0) >= 70) ? 'bg-success' : 'bg-warning text-dark') + '">总分 ' + escapeHtml(String(qs.overall || 0)) + '</span>' +
            '</div>';
        if (!outline.length) {
            html += '<div class="text-muted small">未提取到可用大纲。</div>';
            outlineEl.innerHTML = html;
            return;
        }
        html += '<div class="table-responsive"><table class="table table-sm table-bordered extra-table mb-0">';
        html += '<thead><tr><th style="width:70px;">序号</th><th style="min-width:180px;">一级模块</th><th>子模块</th></tr></thead><tbody>';
        outline.forEach(function(it) {
            var children = Array.isArray(it.children) ? it.children : [];
            html += '<tr><td>' + escapeHtml(String(it.index || '')) + '</td><td>' + escapeHtml(it.title || '') + '</td><td>' + escapeHtml(children.join('；') || '-') + '</td></tr>';
        });
        html += '</tbody></table></div>';
        outlineEl.innerHTML = html;
    }

    function renderPrdQuality() {
        if (!qualityEl) return;
        if (!prdQuality || typeof prdQuality !== 'object') {
            qualityEl.innerHTML = '<div class="text-muted small">暂无PRD质量评分数据</div>';
            return;
        }
        var dimensions = prdQuality.dimensions || {};
        var suggestions = Array.isArray(prdQuality.suggestions) ? prdQuality.suggestions : [];
        var risks = prdQuality.risk_counts || {};
        var html = '';
        html += '<div class="d-flex align-items-center flex-wrap gap-2 mb-2">';
        html += '<span class="badge bg-primary">总分 ' + escapeHtml(String(prdQuality.overall_score || 0)) + '</span>';
        html += '<span class="badge bg-secondary">等级 ' + escapeHtml(String(prdQuality.grade || '-')) + '</span>';
        html += '<span class="badge bg-danger">P0=' + escapeHtml(String(risks.P0 || 0)) + '</span>';
        html += '<span class="badge bg-warning text-dark">P1=' + escapeHtml(String(risks.P1 || 0)) + '</span>';
        html += '<span class="badge bg-light text-dark border">P2=' + escapeHtml(String(risks.P2 || 0)) + '</span>';
        html += '</div>';
        html += '<div class="table-responsive mb-2"><table class="table table-sm table-bordered extra-table mb-0">';
        html += '<thead><tr><th style="width:150px;">维度</th><th>分值(0-100)</th></tr></thead><tbody>';
        Object.keys(dimensions).forEach(function(k) {
            html += '<tr><td>' + escapeHtml(k) + '</td><td>' + escapeHtml(String(dimensions[k])) + '</td></tr>';
        });
        html += '</tbody></table></div>';
        if (suggestions.length) {
            html += '<div class="fw-semibold small mb-1">改进建议</div><ul class="small mb-0">';
            suggestions.forEach(function(s) { html += '<li>' + escapeHtml(s) + '</li>'; });
            html += '</ul>';
        }
        qualityEl.innerHTML = html;
    }

    function renderRiskPrediction() {
        if (!riskPredictionEl) return;
        if (!riskPrediction || typeof riskPrediction !== 'object') {
            riskPredictionEl.innerHTML = '<div class="text-muted small">暂无风险预测数据</div>';
            return;
        }
        var signals = riskPrediction.signals || {};
        var evidences = Array.isArray(riskPrediction.evidence) ? riskPrediction.evidence : [];
        var keyRisks = Array.isArray(riskPrediction.key_risks) ? riskPrediction.key_risks : [];
        var html = '';
        html += '<div class="d-flex align-items-center gap-2 mb-2"><button type="button" class="btn btn-sm btn-outline-secondary btn-clear-linkage">清除联动高亮</button><span class="small text-muted linkage-status">' + (activeLinkedTestPointId ? ('当前联动测试点：' + escapeHtml(activeLinkedTestPointId)) : '当前未联动') + '</span></div>';
        html += '<div class="d-flex align-items-center flex-wrap gap-2 mb-2">';
        html += '<span class="badge ' + (riskPrediction.overall_level === '高' ? 'bg-danger' : (riskPrediction.overall_level === '中' ? 'bg-warning text-dark' : 'bg-success')) + '">总体风险 ' + escapeHtml(riskPrediction.overall_level || '-') + '</span>';
        html += '<span class="badge bg-secondary">概率 ' + escapeHtml(String(riskPrediction.overall_probability || 0)) + '</span>';
        html += '<span class="badge bg-light text-dark border">P0=' + escapeHtml(String(signals.p0 || 0)) + '</span>';
        html += '<span class="badge bg-light text-dark border">P1=' + escapeHtml(String(signals.p1 || 0)) + '</span>';
        html += '<span class="badge bg-light text-dark border">P2=' + escapeHtml(String(signals.p2 || 0)) + '</span>';
        html += '</div>';
        if (evidences.length) {
            html += '<div class="small mb-2"><span class="fw-semibold">证据：</span>' + escapeHtml(evidences.join('；')) + '</div>';
        }
        if (keyRisks.length) {
            html += '<div class="table-responsive"><table class="table table-sm table-bordered extra-table mb-0">';
            html += '<thead><tr><th style="width:140px;">风险项</th><th style="width:100px;">模块</th><th style="width:70px;">等级</th><th style="width:80px;">概率</th><th style="width:160px;">影响路径</th><th style="width:120px;">关联测试点</th><th>原因</th></tr></thead><tbody>';
            keyRisks.forEach(function(r) {
                var tps = Array.isArray(r.related_test_points) ? r.related_test_points : [];
                var tpHtml = tps.length ? tps.map(function(tp){ return '<button type="button" class="btn btn-link p-0 align-baseline rp-testpoint-link" data-tpid="' + escapeHtml(tp) + '">' + escapeHtml(tp) + '</button>'; }).join('、') : '-';
                html += '<tr class="risk-row" data-related="' + escapeHtml(tps.join(',')) + '"><td>' + escapeHtml(r.title || '-') + '</td><td>' + escapeHtml(r.module || '-') + '</td><td>' + escapeHtml(r.risk_level || '-') + '</td><td>' + escapeHtml(String(r.probability || 0)) + '</td><td>' + escapeHtml(r.impact_path || '-') + '</td><td>' + tpHtml + '</td><td>' + escapeHtml(r.reason || '-') + '</td></tr>';
            });
            html += '</tbody></table></div>';
        } else {
            html += '<div class="text-muted small">暂无关键风险项。</div>';
        }
        riskPredictionEl.innerHTML = html;
        applyRiskTestPointHighlight();
    }

    function renderUnderstandingCards() {
        if (!understandingCardsEl) return;
        if (!understandingCards || typeof understandingCards !== 'object') {
            understandingCardsEl.innerHTML = '<div class="text-muted small">暂无理解卡片数据</div>';
            return;
        }
        var cards = Array.isArray(understandingCards.cards) ? understandingCards.cards : [];
        if (!cards.length) {
            understandingCardsEl.innerHTML = '<div class="text-muted small">暂无理解卡片数据</div>';
            return;
        }
        var html = '<div class="small text-muted mb-2">共 ' + escapeHtml(String(understandingCards.card_count || cards.length)) + ' 张卡片</div>';
        cards.forEach(function(c) {
            var flow = Array.isArray(c.core_flow) ? c.core_flow : [];
            var states = Array.isArray(c.key_states) ? c.key_states : [];
            var risks = Array.isArray(c.risk_points) ? c.risk_points : [];
            var qs = Array.isArray(c.open_questions) ? c.open_questions : [];
            html += '<div class="border rounded p-2 mb-2">';
            html += '<div class="fw-semibold mb-1">' + escapeHtml((c.feature_id || '-') + ' ' + (c.feature_name || '-')) + '</div>';
            html += '<div class="small mb-1"><span class="text-muted">用户目标：</span>' + escapeHtml(c.user_goal || '-') + '</div>';
            html += '<div class="small mb-1"><span class="text-muted">系统目标：</span>' + escapeHtml(c.system_goal || '-') + '</div>';
            html += '<div class="small mb-1"><span class="text-muted">核心流程：</span>' + escapeHtml(flow.join(' → ') || '-') + '</div>';
            html += '<div class="small mb-1"><span class="text-muted">关键状态：</span>' + escapeHtml(states.join('、') || '-') + '</div>';
            html += '<div class="small mb-1"><span class="text-muted">风险点：</span>' + escapeHtml(risks.join('；') || '-') + '</div>';
            html += '<div class="small"><span class="text-muted">开放问题：</span>' + escapeHtml(qs.join('；') || '-') + '</div>';
            html += '</div>';
        });
        understandingCardsEl.innerHTML = html;
    }

    function renderReleaseGate() {
        if (!releaseGateEl) return;
        if (!releaseGate || typeof releaseGate !== 'object') {
            releaseGateEl.innerHTML = '<div class="text-muted small">暂无发布门禁数据</div>';
            return;
        }
        var reasons = Array.isArray(releaseGate.reasons) ? releaseGate.reasons : [];
        var fixes = Array.isArray(releaseGate.must_fix) ? releaseGate.must_fix : [];
        var signals = releaseGate.signals || {};
        var thresholds = releaseGate.thresholds || {};
        var decision = String(releaseGate.decision || 'REVIEW');
        var badge = 'bg-warning text-dark';
        if (decision === 'PASS') badge = 'bg-success';
        if (decision === 'BLOCK') badge = 'bg-danger';
        var html = '';
        html += '<div class="d-flex align-items-center gap-2 mb-2">';
        html += '<span class="badge ' + badge + '">决策 ' + escapeHtml(decision) + '</span>';
        html += '<span class="badge bg-light text-dark border">评分 ' + escapeHtml(String(releaseGate.score || 0)) + '</span>';
        html += '</div>';
        html += '<div class="small mb-2 text-muted">P0=' + escapeHtml(String(signals.p0_count || 0)) + '，平台风险=' + escapeHtml(String(signals.platform_risk_count || 0)) + '，质量分=' + escapeHtml(String(signals.quality_score || 0)) + '</div>';
        if (thresholds && typeof thresholds === 'object' && Object.keys(thresholds).length) {
            html += '<div class="small mb-2 text-muted">阈值：P0阻断≥' + escapeHtml(String(thresholds.p0_block_threshold || 1)) + '，平台阻断≥' + escapeHtml(String(thresholds.platform_risk_block_threshold || 7)) + '，质量复审<' + escapeHtml(String(thresholds.quality_review_threshold || 70)) + '</div>';
        }
        html += '<div class="fw-semibold small mb-1">决策原因</div><ul class="small mb-2">' + reasons.map(function(x){ return '<li>' + escapeHtml(x) + '</li>'; }).join('') + '</ul>';
        html += '<div class="fw-semibold small mb-1">必须项</div><ul class="small mb-0">' + fixes.map(function(x){ return '<li>' + escapeHtml(x) + '</li>'; }).join('') + '</ul>';
        releaseGateEl.innerHTML = html;
    }

    function renderDecisionPanelMain() {
        if (!decisionPanelMainEl) return;
        if (!releaseGate || typeof releaseGate !== 'object') {
            decisionPanelMainEl.style.display = 'none';
            decisionPanelMainEl.className = 'decision-panel';
            decisionPanelMainEl.innerHTML = '';
            return;
        }
        var decision = String(releaseGate.decision || 'REVIEW').toUpperCase();
        var signals = releaseGate.signals || {};
        var reasons = Array.isArray(releaseGate.reasons) ? releaseGate.reasons : [];
        var fixes = Array.isArray(releaseGate.must_fix) ? releaseGate.must_fix : [];
        var cls = 'review';
        var badge = 'bg-warning text-dark';
        if (decision === 'PASS') { cls = 'pass'; badge = 'bg-success'; }
        if (decision === 'BLOCK') { cls = 'block'; badge = 'bg-danger'; }
        var html = '';
        html += '<div class="d-flex justify-content-between align-items-start gap-2 mb-2">';
        html += '<div><div class="decision-title">发布决策面板</div><div class="decision-sub">先看结论，再看原因与整改项</div></div>';
        html += '<span class="badge ' + badge + '">' + escapeHtml(decision) + '</span>';
        html += '</div>';
        html += '<div class="small mb-2">评分 <span class="fw-semibold">' + escapeHtml(String(releaseGate.score || 0)) + '</span>，P0=' + escapeHtml(String(signals.p0_count || 0)) + '，平台风险=' + escapeHtml(String(signals.platform_risk_count || 0)) + '</div>';
        if (reasons.length) {
            html += '<div class="small mb-2"><span class="fw-semibold">关键原因：</span>' + escapeHtml(reasons[0]) + '</div>';
        }
        if (fixes.length) {
            html += '<div class="small mb-2"><span class="fw-semibold">整改清单：</span>' + escapeHtml(fixes.slice(0, 2).join('；')) + '</div>';
        }
        html += '<div class="d-flex flex-wrap gap-2">';
        html += '<button type="button" class="btn btn-sm btn-outline-primary btn-open-left-tab" data-target="#leftPaneGate">查看门禁详情</button>';
        html += '<button type="button" class="btn btn-sm btn-outline-secondary btn-open-left-tab" data-target="#leftPaneUnderstanding">查看理解卡片</button>';
        html += '<button type="button" class="btn btn-sm btn-outline-secondary btn-open-left-tab" data-target="#leftPaneImpact">查看平台影响</button>';
        html += '<button type="button" class="btn btn-sm btn-outline-dark btn-copy-must-fix">复制整改清单</button>';
        html += '</div>';
        decisionPanelMainEl.className = 'decision-panel ' + cls;
        decisionPanelMainEl.style.display = 'block';
        decisionPanelMainEl.innerHTML = html;
    }

    function renderReaderGuideMain() {
        if (!readerGuideMainWrapEl || !readerGuideMainContentEl) return;
        if (!readerGuide || typeof readerGuide !== 'object') {
            readerGuideMainWrapEl.style.display = 'none';
            readerGuideMainContentEl.innerHTML = '';
            return;
        }
        var oneLiner = String(readerGuide.one_liner || '').trim();
        var quick = Array.isArray(readerGuide.quick_read_path) ? readerGuide.quick_read_path : [];
        var glossary = Array.isArray(readerGuide.glossary) ? readerGuide.glossary : [];
        var pending = Array.isArray(readerGuide.pending_questions) ? readerGuide.pending_questions : [];
        if (!oneLiner && !quick.length && !glossary.length && !pending.length) {
            readerGuideMainWrapEl.style.display = 'none';
            readerGuideMainContentEl.innerHTML = '';
            return;
        }
        var html = '';
        if (oneLiner) {
            html += '<div class="mb-2"><span class="fw-semibold">一句话结论：</span>' + escapeHtml(oneLiner) + '</div>';
        }
        if (quick.length) {
            html += '<div class="fw-semibold mb-1">阅读顺序</div><ol class="mb-2">';
            quick.slice(0, 3).forEach(function(x){ html += '<li>' + escapeHtml(String(x || '')) + '</li>'; });
            html += '</ol>';
        }
        if (glossary.length) {
            html += '<div class="fw-semibold mb-1">关键术语（避免理解偏差）</div>';
            html += '<div class="table-responsive mb-2"><table class="table table-sm table-bordered extra-table mb-0">';
            html += '<thead><tr><th style="width:220px;">术语</th><th>解释</th></tr></thead><tbody>';
            glossary.slice(0, 6).forEach(function(g) {
                html += '<tr><td>' + escapeHtml(String(g.term || '-')) + '</td><td>' + escapeHtml(String(g.definition || '-')) + '</td></tr>';
            });
            html += '</tbody></table></div>';
        }
        if (pending.length) {
            html += '<div class="fw-semibold mb-1">当前最该澄清的问题</div><ul class="mb-0">';
            pending.slice(0, 3).forEach(function(p) {
                var text = '[' + String(p.priority || 'P2') + '] ' + String(p.module || '-') + '：' + String(p.question || '-');
                html += '<li>' + escapeHtml(text) + '</li>';
            });
            html += '</ul>';
        }
        readerGuideMainContentEl.innerHTML = html;
        readerGuideMainWrapEl.style.display = 'block';
    }

    function renderSharedSummaryMain() {
        if (!sharedSummaryMainWrapEl || !sharedSummaryMainContentEl) return;
        if (!sharedSummary || typeof sharedSummary !== 'object') {
            sharedSummaryMainWrapEl.style.display = 'none';
            sharedSummaryMainContentEl.innerHTML = '';
            return;
        }
        var purpose = String(sharedSummary.purpose || '').trim();
        var scope = Array.isArray(sharedSummary.scope) ? sharedSummary.scope : [];
        var flow = Array.isArray(sharedSummary.core_flow) ? sharedSummary.core_flow : [];
        var points = Array.isArray(sharedSummary.key_points) ? sharedSummary.key_points : [];
        if (!purpose && !scope.length && !flow.length && !points.length) {
            sharedSummaryMainWrapEl.style.display = 'none';
            sharedSummaryMainContentEl.innerHTML = '';
            return;
        }
        
        var titleStr = String(sharedSummary.title || '全员共识摘要');
        var wrapTitleEl = sharedSummaryMainWrapEl.querySelector('.feature-map-main-title');
        if (wrapTitleEl) {
            wrapTitleEl.innerHTML = '<i class="fas fa-users-viewfinder me-2"></i>' + escapeHtml(titleStr);
        }

        var html = '';
        if (purpose) {
            html += '<div class="fw-semibold mb-1 text-primary">1. 文档核心目的</div>';
            html += '<div class="mb-3 ps-3 border-start border-primary border-2 text-muted">' + escapeHtml(purpose) + '</div>';
        }
        if (scope.length) {
            html += '<div class="fw-semibold mb-1 text-primary">2. 覆盖功能范围</div>';
            html += '<div class="mb-3 ps-3 border-start border-primary border-2 text-muted">';
            html += '核心功能：' + escapeHtml(scope.slice(0, 5).join('、'));
            html += '</div>';
        }
        if (flow.length) {
            html += '<div class="fw-semibold mb-1 text-primary">3. 典型主流程逻辑</div>';
            html += '<div class="mb-3 ps-3 border-start border-primary border-2 text-muted">';
            flow.slice(0, 3).forEach(function(x){ 
                html += '<div class="mb-1">' + escapeHtml(String(x || '')) + '</div>'; 
            });
            html += '</div>';
        }
        if (points.length) {
            html += '<div class="fw-semibold mb-1 text-primary">4. 关键技术/业务口径（红线）</div>';
            html += '<div class="mb-0 ps-3 border-start border-primary border-2 text-muted">';
            points.slice(0, 3).forEach(function(x){ 
                html += '<div class="mb-1">' + escapeHtml(String(x || '')) + '</div>'; 
            });
            html += '</div>';
        }
        sharedSummaryMainContentEl.innerHTML = html;
        sharedSummaryMainWrapEl.style.display = 'block';
    }

    function showLeftTab(target) {
        var btn = document.querySelector('button[data-bs-target="' + target + '"]');
        if (!btn || !window.bootstrap || !window.bootstrap.Tab) return;
        var tab = new window.bootstrap.Tab(btn);
        tab.show();
    }

    function applyRiskTestPointHighlight() {
        var tpid = String(activeLinkedTestPointId || '').trim();
        document.querySelectorAll('.linkage-status').forEach(function(el) {
            el.textContent = tpid ? ('当前联动测试点：' + tpid) : '当前未联动';
        });
        document.querySelectorAll('.tp-row').forEach(function(el) {
            var rowId = String(el.getAttribute('data-tp-id') || '').trim();
            el.classList.toggle('table-warning', !!tpid && rowId === tpid);
        });
        document.querySelectorAll('.risk-row').forEach(function(el) {
            var related = String(el.getAttribute('data-related') || '');
            var arr = related ? related.split(',').map(function(s){ return String(s || '').trim(); }).filter(Boolean) : [];
            el.classList.toggle('table-warning', !!tpid && arr.indexOf(tpid) >= 0);
        });
    }

    function renderPlatformImpact() {
        if (!impactEl) return;
        if (!platformImpact || typeof platformImpact !== 'object') {
            impactEl.innerHTML = '<div class="text-muted small">暂无平台影响分析数据</div>';
            if (platformImpactMainWrapEl) platformImpactMainWrapEl.style.display = 'none';
            return;
        }
        var rows = Array.isArray(platformImpact.platform_impacts) ? platformImpact.platform_impacts : [];
        var matrix = Array.isArray(platformImpact.compatibility_matrix) ? platformImpact.compatibility_matrix : [];
        var html = '';
        html += '<div class="small text-muted mb-2">' + escapeHtml(platformImpact.summary || '暂无结论') + '</div>';
        if (platformImpact.retrieval_backend) {
            html += '<div class="small text-muted mb-2">检索后端：' + escapeHtml(platformImpact.retrieval_backend) + '</div>';
        }
        if (rows.length) {
            html += '<div class="table-responsive mb-2"><table class="table table-sm table-bordered extra-table mb-0">';
            html += '<thead><tr><th style="width:120px;">平台</th><th style="width:90px;">风险数</th><th style="width:90px;">检索分</th><th>风险详情</th></tr></thead><tbody>';
            rows.forEach(function(r) {
                var risks = Array.isArray(r.matched_risks) ? r.matched_risks : [];
                var txt = risks.map(function(it) {
                    var evidence = Array.isArray(it.evidence_terms) ? it.evidence_terms.join('、') : '';
                    var score = Number(it.retrieval_score || 0).toFixed(3);
                    var s = '[' + (it.severity || 'P2') + '] ' + (it.feature || '') + '：' + (it.risk || '');
                    if (evidence) s += '（证据词：' + evidence + '）';
                    s += ' [score=' + score + ']';
                    return s;
                }).join('；');
                html += '<tr><td>' + escapeHtml(r.platform || '-') + '</td><td>' + escapeHtml(String(r.risk_count || 0)) + '</td><td>' + escapeHtml(Number(r.retrieval_score || 0).toFixed(3)) + '</td><td>' + escapeHtml(txt || '无') + '</td></tr>';
            });
            html += '</tbody></table></div>';
        }
        if (matrix.length) {
            html += '<div class="table-responsive"><table class="table table-sm table-striped extra-table mb-0">';
            html += '<thead><tr><th>平台</th><th>兼容性</th><th>命中特征</th><th>检索分</th></tr></thead><tbody>';
            matrix.forEach(function(m) {
                var fs = Array.isArray(m.features) ? m.features.join('、') : '';
                html += '<tr><td>' + escapeHtml(m.platform || '-') + '</td><td>' + escapeHtml(m.compatibility || '-') + '</td><td>' + escapeHtml(fs || '-') + '</td><td>' + escapeHtml(Number(m.score || 0).toFixed(3)) + '</td></tr>';
            });
            html += '</tbody></table></div>';
        }
        impactEl.innerHTML = html || '<div class="text-muted small">暂无平台影响分析数据</div>';

        if (platformImpactMainWrapEl && platformImpactMainContentEl) {
            platformImpactMainWrapEl.style.display = 'block';
            platformImpactMainContentEl.innerHTML = html;
        }
    }

    function renderDependencyAnalysis() {
        if (!dependencyEl) return;
        if (!dependencyAnalysis || typeof dependencyAnalysis !== 'object') {
            dependencyEl.innerHTML = '<div class="text-muted small">暂无需求依赖分析数据</div>';
            return;
        }
        var edges = Array.isArray(dependencyAnalysis.edges) ? dependencyAnalysis.edges : [];
        var risks = Array.isArray(dependencyAnalysis.risk_links) ? dependencyAnalysis.risk_links : [];
        var html = '';
        html += '<div class="small text-muted mb-2">' + escapeHtml(dependencyAnalysis.summary || '暂无结论') + '</div>';
        if (risks.length) {
            html += '<div class="table-responsive mb-2"><table class="table table-sm table-bordered extra-table mb-0">';
            html += '<thead><tr><th style="width:120px;">源模块</th><th style="width:120px;">目标模块</th><th style="width:100px;">关系</th><th style="width:80px;">强度</th></tr></thead><tbody>';
            risks.forEach(function(r) {
                html += '<tr><td>' + escapeHtml(r.source || '-') + '</td><td>' + escapeHtml(r.target || '-') + '</td><td>' + escapeHtml(r.reason || '-') + '</td><td>' + escapeHtml(Number(r.strength || 0).toFixed(2)) + '</td></tr>';
            });
            html += '</tbody></table></div>';
        }
        if (edges.length) {
            html += '<div class="fw-semibold mb-1 small">依赖关系图</div><div id="dependencyGraphMermaid" class="mb-2"></div>';
        } else {
            html += '<div class="text-muted small">暂无关系边。</div>';
        }
        dependencyEl.innerHTML = html;
        if (edges.length) {
            renderMermaidBlock(dependencyAnalysis.dependency_graph_mermaid || '', 'dependencyGraphMermaid');
        }
    }

    function buildFeatureMindmapCode() {
        var rows = buildFeatureNodes();
        if (!rows.length) return '';

        var lines = ['mindmap', '  root((PRD功能清单))'];
        rows.forEach(function(r) {
            var lvl = Math.max(1, Math.min(4, Number(r.level || 1)));
            var indent = '  ' + '  '.repeat(lvl);
            lines.push(indent + cleanMindLabel(r.title));
        });
        return lines.join('\n');
    }

    function renderFeatureMindmap() {
        if (!featureMapEl) return;
        featureMindmapCode = buildFeatureMindmapCode();
        if (!featureMindmapCode) {
            featureMapEl.innerHTML = '<div class="text-muted small">暂无可提取的功能结构，请先执行 PRD 分析。</div>';
            if (featureMapMainEl) featureMapMainEl.innerHTML = '<div class="text-muted small p-2">暂无可提取的功能结构</div>';
            if (featureMapMainMetaEl) featureMapMainMetaEl.textContent = '';
            if (featureMapMainWrapEl) featureMapMainWrapEl.style.display = 'none';
            return;
        }
        renderMermaidBlock(featureMindmapCode, 'featureMapContent');
        if (featureMapMainWrapEl) featureMapMainWrapEl.style.display = 'block';
        if (featureMapMainMetaEl) {
            var q = outlineEngine && outlineEngine.quality_score ? outlineEngine.quality_score : {};
            var meta = '模式：' + (outlineEngine && outlineEngine.mode ? outlineEngine.mode : 'structured');
            if (q && q.overall != null) meta += '｜结构质量分：' + String(q.overall);
            featureMapMainMetaEl.textContent = meta;
        }
        if (featureMapMainEl) renderMermaidBlock(featureMindmapCode, 'featureMapMainContent');
    }

    function buildFourPillarsHtmlFromJson(o) {
        if (!o || typeof o !== 'object') return '<div class="text-muted">无数据</div>';
        var md = String(o.outline_markdown || '').trim();
        // 若模型同时返回 role_views，则前端优先展示“角色视图”，避免 markdown 覆盖掉角色切换区
        var hasRoleViews = o.role_views && typeof o.role_views === 'object';
        if (!hasRoleViews && md && typeof marked !== 'undefined') {
            return '<div class="markdown-body">' + marked.parse(md) + '</div>';
        }
        
        var fp = o.four_pillars || {};
        var titles = {
            collaboration_roles: '协作与角色',
            capabilities_constraints: '能力与约束',
            exceptions_recovery: '异常与恢复',
            delivery_rollout: '交付与上线'
        };
        var html = '';
        if (o.document_title) html += '<div class="fw-bold mb-2">' + escapeHtml(o.document_title) + '</div>';
        
        // 使用 Bootstrap 的 Accordion (折叠面板) 包装四支柱内容
        html += '<div class="accordion accordion-flush border rounded" id="accordionFourPillars">';
        html += '  <div class="accordion-item">';
        html += '    <h2 class="accordion-header" id="headingFourPillars">';
        html += '      <button class="accordion-button collapsed py-2 px-3 bg-light text-secondary small fw-bold" type="button" data-bs-toggle="collapse" data-bs-target="#collapseFourPillars" aria-expanded="false" aria-controls="collapseFourPillars">';
        html += '        <i class="fas fa-robot me-2"></i>四支柱结构化底座（AI 解析中间产物）';
        html += '      </button>';
        html += '    </h2>';
        html += '    <div id="collapseFourPillars" class="accordion-collapse collapse" aria-labelledby="headingFourPillars" data-bs-parent="#accordionFourPillars">';
        html += '      <div class="accordion-body p-3">';
        html += '        <div class="text-muted small mb-3"><i class="fas fa-info-circle me-1"></i>此为大模型提取的结构化参数，主要用于支撑底层缺陷扫描规则，非面向用户的最终报告。</div>';
        
        Object.keys(titles).forEach(function(k) {
            var sec = fp[k];
            if (!sec || typeof sec !== 'object') return;
            html += '<div class="mb-3"><div class="fw-semibold text-primary">' + escapeHtml(titles[k]) + '</div>';
            if (sec.summary) html += '<p class="mb-1 small">' + escapeHtml(sec.summary) + '</p>';
            var items = Array.isArray(sec.items) ? sec.items : [];
            if (items.length) {
                html += '<ul class="small mb-0 text-muted">';
                items.forEach(function(it) {
                    if (!it || typeof it !== 'object') return;
                    var line = '';
                    if (it.name != null && it.responsibility != null) line = escapeHtml(String(it.name)) + '：' + escapeHtml(String(it.responsibility));
                    else if (it.topic != null && it.detail != null) line = escapeHtml(String(it.topic)) + '：' + escapeHtml(String(it.detail));
                    else if (it.scenario != null) line = escapeHtml(String(it.scenario)) + ' → ' + escapeHtml(String(it.expected_behavior != null ? it.expected_behavior : ''));
                    else if (it.milestone != null) line = escapeHtml(String(it.milestone)) + '：' + escapeHtml(String(it.detail != null ? it.detail : ''));
                    else line = escapeHtml(JSON.stringify(it));
                    html += '<li>' + line + '</li>';
                });
                html += '</ul>';
            }
            html += '</div>';
        });
        
        html += '      </div>';
        html += '    </div>';
        html += '  </div>';
        html += '</div>';
        
        var gaps = Array.isArray(o.gaps) ? o.gaps : [];
        if (gaps.length) {
            html += '<div class="mt-3"><div class="fw-semibold text-warning">待确认 / 缺口</div><ul class="small mb-0">';
            gaps.forEach(function(g) { 
                var issue = typeof g === 'object' ? String(g.issue || '') : String(g || '');
                var collab = typeof g === 'object' ? String(g.suggested_collaboration || '') : '';
                var text = issue;
                if (collab && collab !== '无' && collab !== '无需') {
                    text += ' <span class="badge bg-light text-secondary border ms-1"><i class="fas fa-users me-1"></i>建议拉通：' + escapeHtml(collab) + '</span>';
                }
                html += '<li class="mb-1">' + text + '</li>'; 
            });
            html += '</ul></div>';
        }
        return html || '<div class="text-muted">无结构化数据</div>';
    }

    function renderLlmFourPillarsBlock() {
        if (!llmFourPillarsWrapEl || !llmFourPillarsBodyEl) return;
        if (!llmFourPillarsPayload || !llmFourPillarsPayload.llm) {
            llmFourPillarsWrapEl.style.display = 'none';
            return;
        }
        var llm = llmFourPillarsPayload.llm;
        llmFourPillarsWrapEl.style.display = 'block';
        if (llmOutlineStatusEl) {
            llmOutlineStatusEl.textContent = llm.ok ? '生成成功' : (llm.error ? '失败' : '未完成');
        }
        if (!llm.ok) {
            var errHtml = '<div class="text-danger small">' + escapeHtml(String(llm.error || '未知错误')) + '</div>';
            if (llm.raw_response) errHtml += '<pre class="small mt-2 mb-0 text-muted text-break" style="max-height:240px;overflow:auto;">' + escapeHtml(String(llm.raw_response).slice(0, 4000)) + '</pre>';
            llmFourPillarsBodyEl.innerHTML = errHtml;
            return;
        }
        var o = llm.llm_outline || {};
        // 1) 优先展示角色视图（快速看懂、认知拉齐）
        var roleViews = o.role_views && typeof o.role_views === 'object' ? o.role_views : null;
        if (roleViews) {
            var roleKeyOrder = [
                { k: 'product_manager', label: '产品经理' },
                { k: 'engineering_lead', label: '研发负责人' },
                { k: 'test_engineer', label: '测试' },
                { k: 'interaction_designer', label: '交互设计' }
            ];
            var defaultRole = 'product_manager';
            var initialRole = defaultRole;
            var btnHtml = '';
            roleKeyOrder.forEach(function(rp, idx) {
                var active = rp.k === initialRole;
                btnHtml += '<button type="button" class="btn btn-sm ' + (active ? 'btn-primary' : 'btn-outline-secondary') + ' me-1 mb-1" data-llm-role="' + escapeHtml(rp.k) + '">' + escapeHtml(rp.label) + '</button>';
            });
            var contentHtml = buildRoleViewHtml(roleViews, initialRole, o.gaps);
            llmFourPillarsBodyEl.innerHTML =
                '<div class="mb-2">' +
                    '<div class="fw-semibold text-primary mb-2"><i class="fas fa-users me-1"></i>角色视图（快速看懂 PRD）</div>' +
                    '<div class="mb-2">' + btnHtml + '</div>' +
                    '<div id="llmRoleViewContent">' + contentHtml + '</div>' +
                '</div>';
            // 绑定按钮事件：切换角色视图
            try {
                var roleBtns = llmFourPillarsBodyEl.querySelectorAll('button[data-llm-role]');
                Array.prototype.forEach.call(roleBtns, function(btn) {
                    btn.addEventListener('click', function() {
                        var role = String(this.getAttribute('data-llm-role') || '');
                        if (!role) return;
                        roleBtns.forEach(function(b) {
                            b.classList.toggle('btn-primary', b === btn);
                            b.classList.toggle('btn-outline-secondary', b !== btn);
                        });
                        var newHtml = buildRoleViewHtml(roleViews, role, o.gaps);
                        var cEl = document.getElementById('llmRoleViewContent');
                        if (cEl) cEl.innerHTML = newHtml;
                    });
                });
            } catch (e) {}
        } else {
            // 2) 退化：仅展示四支柱
            llmFourPillarsBodyEl.innerHTML = buildFourPillarsHtmlFromJson(o);
        }
        if (llmFourPillarsPayload.merged && typeof llmFourPillarsPayload.merged === 'object') {
            llmFourPillarsBodyEl.innerHTML += '<div class="mt-2 small text-muted border-top pt-2">已请求合并本地大纲：完整合并结果在接口 JSON 的 <code>merged</code> 字段中。</div>';
        }
    }

    function renderFourPillarsAuditBlock(level) {
        if (!fourPillarsAuditWrapEl || !fourPillarsAuditContentEl) return;
        var lv = String(level || reportLevel || 'L3');
        if (lv === 'OUTLINE' || lv === 'VALIDATION' || lv === 'IMPACT') {
            fourPillarsAuditWrapEl.style.display = 'none';
            return;
        }
        var llm = llmFourPillarsPayload && llmFourPillarsPayload.llm;
        if (!llm || !llm.ok || !llm.llm_outline) {
            fourPillarsAuditWrapEl.style.display = 'none';
            return;
        }
        fourPillarsAuditContentEl.innerHTML = buildFourPillarsHtmlFromJson(llm.llm_outline || {});
        fourPillarsAuditWrapEl.style.display = 'block';
    }

    function buildRoleViewHtml(roleViews, roleKey, gaps) {
        if (!roleViews || typeof roleViews !== 'object') return '<div class="text-muted small">无角色视图数据</div>';
        var rv = roleViews[roleKey];
        if (!rv || typeof rv !== 'object') return '<div class="text-muted small">该角色无可展示内容</div>';
        var headline = String(rv.headline || '').trim();
        var points = Array.isArray(rv.focus_points) ? rv.focus_points : [];
        var g = Array.isArray(gaps) ? gaps : [];
        var html = '';
        if (headline) {
            html += '<div class="fw-semibold mb-2">' + escapeHtml(headline) + '</div>';
        }
        if (points.length) {
            html += '<ul class="mb-2">';
            points.slice(0, 12).forEach(function(p) {
                html += '<li class="small">' + escapeHtml(String(p || '')) + '</li>';
            });
            html += '</ul>';
        }
        if (g.length) {
            html += '<div class="small text-warning"><div class="fw-semibold">待确认（所有角色共享）：</div><ul class="mb-0">';
            g.slice(0, 6).forEach(function(x) {
                var issue = typeof x === 'object' ? String(x.issue || '') : String(x || '');
                var collab = typeof x === 'object' ? String(x.suggested_collaboration || '') : '';
                var text = escapeHtml(issue);
                if (collab && collab !== '无' && collab !== '无需') {
                    text += ' <span class="badge bg-light text-secondary border ms-1"><i class="fas fa-users me-1"></i>建议拉通：' + escapeHtml(collab) + '</span>';
                }
                html += '<li class="mb-1">' + text + '</li>';
            });
            html += '</ul></div>';
        }
        return html || '<div class="text-muted small">无可展示内容</div>';
    }

    function buildReadableOutlineFromLocal(engine) {
        if (!engine || typeof engine !== 'object') return '';
        var systemModel = (engine.system_model && typeof engine.system_model === 'object') ? engine.system_model : {};
        var explicitOutline = (engine.explicit_outline && typeof engine.explicit_outline === 'object') ? engine.explicit_outline : ((systemModel.explicit_outline && typeof systemModel.explicit_outline === 'object') ? systemModel.explicit_outline : {});
        var coreBrief = (engine.core_brief && typeof engine.core_brief === 'object') ? engine.core_brief : ((systemModel.core_brief && typeof systemModel.core_brief === 'object') ? systemModel.core_brief : {});
        var ruleModel = (engine.rule_model && typeof engine.rule_model === 'object') ? engine.rule_model : {};
        var c = (engine.cognitive_outline && typeof engine.cognitive_outline === 'object') ? engine.cognitive_outline : {};
        function cleanHumanText(s) {
            var t = String(s == null ? '' : s);
            // 去除控制字符/奇怪分隔符，修正常见乱码空白
            t = t.replace(/[\u0000-\u001f\u007f]/g, ' ');
            t = t.replace(/[]+/g, ' ');
            t = t.replace(/\s+/g, ' ').trim();
            // 批量替换特殊部首编码为标准汉字
            t = t.replace(/⼀/g, '一').replace(/⾮/g, '非').replace(/⽤/g, '用').replace(/⽰/g, '示').replace(/⽤/g, '用');
            t = t.replace(/⼴/g, '广').replace(/告/g, '告').replace(/⼈/g, '人').replace(/展⽰/g, '展示').replace(/最⾼/g, '最高');
            
            // 去掉常见编号前缀、例如前缀、多余符号
            t = t.replace(/^(\d+\.|[ivx]+\.|-|\*)\s*/i, '');
            t = t.replace(/^(例如|比如|如：|即|注：)/, '');
            t = t.replace(/^(\d+\.\s*[\u4e00-\u9fa5]+：)/, '');
            t = t.replace(/^【[^】]+】\s*/, ''); // 去除【星耀屏】这类前缀
            t = t.replace(/^[a-z]\.\s*/i, ''); // 去除 i. a. 这类前缀
            t = t.replace(/^(ai数字人|数字人|投屏|游戏|广告)\s*(\d+\.\s*)?/i, ''); // 强行剥离角色前缀导致的不通顺
            
            // 清理遗留的特殊符号组合
            t = t.replace(/：，则/g, '，则').replace(/：，/g, '：').replace(/，则/g, '，则');
            t = t.replace(/P[0-9]\s*/g, ''); // 去除 P1 P0 这种优先级标记混入正文
            
            // 修正“时展示…”这类缺主语句式
            // 典型残句改写：时X，则Y / 此时X，则Y / 若X，则Y -> 当X时，会Y
            t = t.replace(/^(此时|当|若|如果)?\s*时?\s*([^，,]+)[，,]\s*则\s*(.+)$/g, function(_, pfx, cond, act){
                cond = String(cond || '').trim();
                act = String(act || '').trim();
                if (!cond || !act) return _;
                return '当' + cond + '时，会' + act;
            });
            if (/^时/.test(t)) t = '当' + t.replace(/^时/, '');
            // 让“则”更像句子（极轻量，不改语义）
            t = t.replace(/，?则/g, '，则');
            t = t.replace(/^，/,'');
            
            // 如果遇到非常长且带有“例如”说明的复合句，截取前半句为主
            if (t.length > 40 && t.indexOf('：则') !== -1) {
                t = t.split('：则')[0] + '：则执行后续操作';
            }
            if (t.length > 60 && t.indexOf('、') !== -1 && t.indexOf('：') !== -1) {
                var parts = t.split('：');
                t = parts[0] + '：' + (parts[1] || '').split('、')[0];
            }
            
            t = t.trim();
            // 如果清理后太短，或者全是顿号分隔的词汇，直接抛弃
            if (t.length < 8) return '';
            if (t.indexOf('展示优先级') !== -1 && t.length < 15) return '';
            if (t.split('、').length > 3 && t.length < 25) return ''; // 纯名词堆砌
            
            // 末尾补句号
            if (t && !/[。！？]$/.test(t)) t += '。';
            return t;
        }
        function pickClean(arr, n, maxLen) {
            var out = [];
            (Array.isArray(arr) ? arr : []).forEach(function(x){
                var t = cleanHumanText(x);
                if (!t) return;
                if (maxLen && t.length > maxLen) return;
                // 过滤掉明显“半句/残片”或纯词汇（非句子）
                if (t.length < 8) return; 
                if (t.indexOf('规范') !== -1 && t.length < 15) return; // 过滤掉“规范展示规则”等短语
                out.push(t);
            });
            // 去重
            var seen = {};
            var dedup = [];
            out.forEach(function(x){
                var k = x.replace(/\s+/g,'');
                if (seen[k]) return;
                seen[k] = 1;
                dedup.push(x);
            });
            return dedup.slice(0, n);
        }
        var chain = pickClean(ruleModel.priority_chain, 10, 80);

        // 一句话理解：优先用“优先级链”生成通顺句，其次才用 L0
        var l0 = '';
        var l0Raw = String(c.L0 || '').trim();
        if (chain.length) {
            l0 = '本 PRD 主要定义展示调度的优先级与打断规则，优先级链为：' + chain.join(' > ').replace(/。/g, '') + '。';
        } else if (l0Raw) {
            l0 = cleanHumanText(l0Raw);
        }

        // 核心角色：优先用 role_duty_table 的 role 字段（更干净）
        var roleTable = Array.isArray(explicitOutline.role_duty_table) ? explicitOutline.role_duty_table : [];
        var roles = [];
        if (roleTable.length) {
            var seenRole = {};
            roleTable.forEach(function(r){
                var name = String((r && r.role) || '').trim();
                if (!name) return;
                if (name.length > 18) return;
                if (seenRole[name]) return;
                seenRole[name] = 1;
                roles.push(name);
            });
            roles = roles.slice(0, 10);
        }
        if (!roles.length) {
            var roleArr = Array.isArray(explicitOutline.roles) ? explicitOutline.roles : (Array.isArray(coreBrief.roles) ? coreBrief.roles : []);
            roles = roleArr.map(function(x){ return String(x || '').trim(); })
                .filter(function(x){ return x && x.length <= 18 && x.indexOf('：') === -1 && x.indexOf('，') === -1; })
                .slice(0, 10);
        }

        // 主流程：优先 flow_step_table.action（更像步骤），其次 main_flow/core_process
        var flowTable = Array.isArray(explicitOutline.flow_step_table) ? explicitOutline.flow_step_table : [];
        var mainFlow = [];
        if (flowTable.length) {
            mainFlow = pickClean(flowTable.map(function(r){ return (r && r.action) ? String(r.action) : ''; }), 6, 120);
        }
        if (!mainFlow.length) mainFlow = pickClean(explicitOutline.main_flow, 6, 140);
        if (!mainFlow.length) mainFlow = pickClean(coreBrief.core_process, 6, 140);

        // 关键打断/恢复：从 exception_table 提取，或 coreBrief.exceptions
        var exceptions = [];
        var exRows = Array.isArray(explicitOutline.exception_table) ? explicitOutline.exception_table : [];
        if (exRows.length) {
            exceptions = pickClean(exRows.map(function(it){
                var a = it && it.scene ? String(it.scene) : '';
                var b = it && it.behavior ? String(it.behavior) : '';
                if (!a && !b) return '';
                return (a ? a : '异常') + '：' + (b || '');
            }), 6, 160);
        }
        if (!exceptions.length) exceptions = pickClean(coreBrief.exceptions, 6, 160);

        var pending = pickClean(explicitOutline.pending_list, 6, 160);
        if (!pending.length) pending = pickClean(coreBrief.todo_confirm, 6, 160);

        if (!l0 && !roles.length && !mainFlow.length && !chain.length && !exceptions.length && !pending.length) return '';

        var html = '';
        if (l0) html += '<div class="mb-2"><span class="fw-semibold">一句话理解：</span>' + escapeHtml(String(l0).replace(/[。]+$/,'')) + '。</div>';
        if (roles.length) html += '<div class="mb-2"><span class="fw-semibold">核心角色：</span>' + escapeHtml(roles.join('、')) + '</div>';
        if (chain.length) html += '<div class="mb-2"><span class="fw-semibold">优先级链：</span>' + escapeHtml(chain.map(function(x){ return String(x).replace(/[。]+$/,''); }).join(' > ')) + '</div>';
        if (mainFlow.length) {
            html += '<div class="fw-semibold mb-1">主流程（人话版）</div><ol class="mb-2">';
            mainFlow.forEach(function(x){ html += '<li>' + escapeHtml(String(x).replace(/[。]+$/,'')) + '。</li>'; });
            html += '</ol>';
        }
        if (exceptions.length) {
            html += '<div class="fw-semibold mb-1">关键打断/恢复</div><ul class="mb-2">';
            exceptions.forEach(function(x){ html += '<li>' + escapeHtml(String(x).replace(/[。]+$/,'')) + '。</li>'; });
            html += '</ul>';
        }
        if (pending.length) html += '<div class="mb-0"><span class="fw-semibold">待确认：</span>' + escapeHtml(pending.map(function(x){ return String(x).replace(/[。]+$/,''); }).join('；')) + '</div>';
        return html;
    }

    function renderMainOutline() {
        if (!contentOutlineMainWrapEl || !contentOutlineMainContentEl) return;
        renderLlmFourPillarsBlock();
        renderSharedSummaryMain();
        var rows = buildFeatureNodes();
        var outline = [];
        if (outlineEngine && Array.isArray(outlineEngine.outline) && outlineEngine.outline.length) {
            outline = outlineEngine.outline;
        }
        var c = (outlineEngine && outlineEngine.cognitive_outline && typeof outlineEngine.cognitive_outline === 'object') ? outlineEngine.cognitive_outline : {};
        var systemType = (outlineEngine && outlineEngine.system_type) ? String(outlineEngine.system_type) : 'general';
        var classifierConfidence = Number((outlineEngine && outlineEngine.classifier_confidence) || 0);
        var ruleModel = (outlineEngine && outlineEngine.rule_model && typeof outlineEngine.rule_model === 'object') ? outlineEngine.rule_model : {};
        var systemModel = (outlineEngine && outlineEngine.system_model && typeof outlineEngine.system_model === 'object') ? outlineEngine.system_model : {};
        var atomicRules = Array.isArray(outlineEngine && outlineEngine.atomic_rules) ? outlineEngine.atomic_rules : (Array.isArray(systemModel.atomic_rules) ? systemModel.atomic_rules : []);
        var ruleDiagnostics = (outlineEngine && outlineEngine.rule_diagnostics && typeof outlineEngine.rule_diagnostics === 'object') ? outlineEngine.rule_diagnostics : {};
        var remediationPlan = Array.isArray(outlineEngine && outlineEngine.remediation_plan) ? outlineEngine.remediation_plan : [];
        var stateMachine = (outlineEngine && outlineEngine.state_machine && typeof outlineEngine.state_machine === 'object') ? outlineEngine.state_machine : ((systemModel && systemModel.state_machine && typeof systemModel.state_machine === 'object') ? systemModel.state_machine : {});
        var deterministicRules = (outlineEngine && outlineEngine.deterministic_rules && typeof outlineEngine.deterministic_rules === 'object') ? outlineEngine.deterministic_rules : {};
        var rulePlugin = (outlineEngine && outlineEngine.rule_plugin && typeof outlineEngine.rule_plugin === 'object') ? outlineEngine.rule_plugin : ((deterministicRules && deterministicRules.plugin && typeof deterministicRules.plugin === 'object') ? deterministicRules.plugin : {});
        var coreBrief = (outlineEngine && outlineEngine.core_brief && typeof outlineEngine.core_brief === 'object') ? outlineEngine.core_brief : ((systemModel && systemModel.core_brief && typeof systemModel.core_brief === 'object') ? systemModel.core_brief : {});
        var explicitOutline = (outlineEngine && outlineEngine.explicit_outline && typeof outlineEngine.explicit_outline === 'object') ? outlineEngine.explicit_outline : ((systemModel && systemModel.explicit_outline && typeof systemModel.explicit_outline === 'object') ? systemModel.explicit_outline : {});
        var promptProfile = (outlineEngine && outlineEngine.prompt_profile && typeof outlineEngine.prompt_profile === 'object') ? outlineEngine.prompt_profile : {};
        var promptEvaluation = (outlineEngine && outlineEngine.prompt_evaluation && typeof outlineEngine.prompt_evaluation === 'object') ? outlineEngine.prompt_evaluation : {};
        var explainableReport = (outlineEngine && outlineEngine.explainable_report && typeof outlineEngine.explainable_report === 'object') ? outlineEngine.explainable_report : {};
        var strategyReport = (outlineEngine && outlineEngine.strategy_report && typeof outlineEngine.strategy_report === 'object') ? outlineEngine.strategy_report : {};
        var stateMermaidCode = String((stateMachine && stateMachine.mermaid) || '');
        var l0 = String(c.L0 || '').trim();
        var l1 = Array.isArray(c.L1) ? c.L1 : [];
        var l2 = Array.isArray(c.L2) ? c.L2 : [];
        var l3 = Array.isArray(c.L3) ? c.L3 : [];
        var l4 = Array.isArray(c.L4) ? c.L4 : [];
        var llmOk = !!(llmFourPillarsPayload && llmFourPillarsPayload.llm && llmFourPillarsPayload.llm.ok);
        if (reportLevel === 'OUTLINE') {
            if (llmOk) {
                if (contentOutlineMainMetaEl) contentOutlineMainMetaEl.textContent = '当前展示全员共识摘要与认知大纲；结构化抽取结果可在「技术审计 (L3)」查看。';
                contentOutlineMainContentEl.innerHTML = '<div class="small text-muted py-2 mt-2 border-top">如需查看结构化抽取、规则底座、状态机与冲突诊断，请切换到「技术审计 (L3)」。</div>';
                contentOutlineMainWrapEl.style.display = 'block';
                return;
            }

            if (contentOutlineMainMetaEl) contentOutlineMainMetaEl.textContent = '当前页签优先展示全员共识摘要与认知大纲。';
            contentOutlineMainContentEl.innerHTML = '<div class="text-muted py-2">本次未加载认知大纲，可前往「技术审计 (L3)」查看结构化抽取与规则分析结果。</div><div class="small text-muted border-top pt-2 mt-2">结构视图、规则底座与状态机等内容统一在「技术审计 (L3)」呈现。</div>';
            contentOutlineMainWrapEl.style.display = 'block';
            return;
        }
        if (!rows.length && !outline.length) {
            if (!hasLlmPayload) {
                contentOutlineMainWrapEl.style.display = 'none';
                return;
            }
            if (contentOutlineMainMetaEl) contentOutlineMainMetaEl.textContent = '';
            contentOutlineMainContentEl.innerHTML = '<div class="text-muted py-2">本地结构化大纲与规则提取将在点击「开始分析」后生成。</div>';
            contentOutlineMainWrapEl.style.display = 'block';
            return;
        }
        var html = '';
        var explainMermaidBlocks = [];
        if ((explicitOutline && typeof explicitOutline === 'object') || (coreBrief && typeof coreBrief === 'object') || l0 || l1.length || l2.length || l3.length || l4.length) {
            if (explicitOutline && typeof explicitOutline === 'object') {
                explicitOutlineLatest = explicitOutline;
                var eCov = (explicitOutline.coverage && typeof explicitOutline.coverage === 'object') ? explicitOutline.coverage : {};
                var eBiz = Array.isArray(explicitOutline.business_summary) ? explicitOutline.business_summary : [];
                var eRoles = Array.isArray(explicitOutline.roles) ? explicitOutline.roles : [];
                var eFlow = Array.isArray(explicitOutline.main_flow) ? explicitOutline.main_flow : [];
                var eRules = Array.isArray(explicitOutline.rules_summary) ? explicitOutline.rules_summary : [];
                var eExRows = Array.isArray(explicitOutline.exception_table) ? explicitOutline.exception_table : [];
                var ePending = Array.isArray(explicitOutline.pending_list) ? explicitOutline.pending_list : [];
                var eDigest = Array.isArray(explicitOutline.alignment_digest) ? explicitOutline.alignment_digest : [];
                var eMissing = Array.isArray(explicitOutline.missing_sections) ? explicitOutline.missing_sections : [];
                var eSignals = (explicitOutline.quality_signals && typeof explicitOutline.quality_signals === 'object') ? explicitOutline.quality_signals : {};
                var eRoleTable = Array.isArray(explicitOutline.role_duty_table) ? explicitOutline.role_duty_table : [];
                var eFlowTable = Array.isArray(explicitOutline.flow_step_table) ? explicitOutline.flow_step_table : [];
                explicitRoleRowsLatest = eRoleTable;
                if (eBiz.length || eRoles.length || eFlow.length || eRules.length || eExRows.length || ePending.length || eRoleTable.length || eFlowTable.length) {
                    html += '<div class="fw-semibold mb-1">结构化抽取视图（规则引擎）</div>';
                    html += '<div class="small text-muted mb-1">覆盖度：' + escapeHtml(String(eCov.level || 'low')) + '（' + escapeHtml(String(eCov.hit || 0)) + '/' + escapeHtml(String(eCov.total || 0)) + '）</div>';
                    html += '<div class="small text-muted mb-1">可读性：' + escapeHtml(String(eSignals.readability_score || 0)) + '｜有效句：' + escapeHtml(String(eSignals.clean_sentence_count || 0)) + '/' + escapeHtml(String(eSignals.sentence_count || 0)) + '</div>';
                    if (eDigest.length) {
                        html += '<div class="mb-1"><span class="fw-semibold">认知拉齐（3分钟）：</span></div><ul class="mb-2">';
                        eDigest.slice(0, 6).forEach(function(x){ html += '<li>' + escapeHtml(String(x || '')) + '</li>'; });
                        html += '</ul>';
                    }
                    if (eMissing.length) {
                        html += '<div class="mb-2 text-warning"><span class="fw-semibold">缺失项：</span>' + escapeHtml(eMissing.join('、')) + '</div>';
                    }
                    if (eBiz.length) html += '<div class="mb-1"><span class="fw-semibold">业务定位：</span>' + escapeHtml(eBiz.join('；')) + '</div>';
                    if (eRoles.length) html += '<div class="mb-1"><span class="fw-semibold">角色分工：</span>' + escapeHtml(eRoles.join('；')) + '</div>';
                    if (eRoleTable.length) {
                        html += '<div class="fw-semibold mb-1">角色-职责表</div>';
                        html += '<div class="table-responsive mb-2"><table class="table table-sm table-bordered extra-table mb-0">';
                        html += '<thead><tr><th style="width:140px;">角色</th><th>职责</th></tr></thead><tbody>';
                        eRoleTable.slice(0, 10).forEach(function(r){
                            html += '<tr><td>' + escapeHtml(String(r.role || '-')) + '</td><td>' + escapeHtml(String(r.duty || '-')) + '</td></tr>';
                        });
                        html += '</tbody></table></div>';
                    }
                    if (eFlow.length) html += '<div class="mb-1"><span class="fw-semibold">主流程：</span>' + escapeHtml(eFlow.join('；')) + '</div>';
                    if (eFlowTable.length) {
                        html += '<div class="fw-semibold mb-1">步骤-输入-输出表</div>';
                        html += '<div class="table-responsive mb-2"><table class="table table-sm table-bordered extra-table mb-0">';
                        html += '<thead><tr><th style="width:70px;">步骤</th><th style="width:140px;">责任角色</th><th>动作</th><th style="width:140px;">输入</th><th style="width:180px;">输出</th></tr></thead><tbody id="explicitOwnerTableBody">';
                        eFlowTable.slice(0, 12).forEach(function(r){
                            html += '<tr data-step="' + escapeHtml(String(r.step || '-')) + '">';
                            html += '<td>' + escapeHtml(String(r.step || '-')) + '</td>';
                            html += '<td><input class="form-control form-control-sm owner-input" value="' + escapeHtml(String(r.owner || '-')) + '"></td>';
                            html += '<td class="action-cell" data-action="' + escapeHtml(String(r.action || '-')) + '">' + escapeHtml(String(r.action || '-')) + '</td>';
                            html += '<td class="input-cell" data-input="' + escapeHtml(String(r.input || '-')) + '">' + escapeHtml(String(r.input || '-')) + '</td>';
                            html += '<td class="output-cell" data-output="' + escapeHtml(String(r.output || '-')) + '">' + escapeHtml(String(r.output || '-')) + '</td>';
                            html += '</tr>';
                        });
                        html += '</tbody></table></div><div class="d-flex align-items-center gap-2 mb-2"><button class="btn btn-sm btn-outline-success" id="btnSaveOwnerCorrection">保存责任角色校正</button><span class="small text-muted" id="ownerCorrectionStatus"></span></div>';
                    }
                    if (eRules.length) html += '<div class="mb-1"><span class="fw-semibold">规则摘要：</span>' + escapeHtml(eRules.join('；')) + '</div>';
                    if (eExRows.length) {
                        html += '<div class="table-responsive mb-2"><table class="table table-sm table-bordered extra-table mb-0">';
                        html += '<thead><tr><th>异常场景</th><th>触发条件</th><th>系统行为</th><th>风险级别</th></tr></thead><tbody>';
                        eExRows.slice(0, 12).forEach(function(r){
                            html += '<tr><td>' + escapeHtml(String(r.scene || '-')) + '</td><td>' + escapeHtml(String(r.trigger || '-')) + '</td><td>' + escapeHtml(String(r.behavior || '-')) + '</td><td>' + escapeHtml(String(r.level || '-')) + '</td></tr>';
                        });
                        html += '</tbody></table></div>';
                    }
                    if (ePending.length) html += '<div class="mb-2"><span class="fw-semibold">待确认：</span>' + escapeHtml(ePending.join('；')) + '</div>';
                }
            }
            if (coreBrief && typeof coreBrief === 'object') {
                var cov = (coreBrief.coverage && typeof coreBrief.coverage === 'object') ? coreBrief.coverage : {};
                var pos = Array.isArray(coreBrief.positioning) ? coreBrief.positioning : [];
                var roles = Array.isArray(coreBrief.roles) ? coreBrief.roles : [];
                var pay = Array.isArray(coreBrief.payment_model) ? coreBrief.payment_model : [];
                var proc = Array.isArray(coreBrief.core_process) ? coreBrief.core_process : [];
                var integ = Array.isArray(coreBrief.integration) ? coreBrief.integration : [];
                var expt = Array.isArray(coreBrief.exceptions) ? coreBrief.exceptions : [];
                var impl = Array.isArray(coreBrief.implementation) ? coreBrief.implementation : [];
                var risks = Array.isArray(coreBrief.risks) ? coreBrief.risks : [];
                var todo = Array.isArray(coreBrief.todo_confirm) ? coreBrief.todo_confirm : [];
                if (pos.length || roles.length || pay.length || proc.length || integ.length || expt.length || impl.length || risks.length || todo.length) {
                    html += '<div class="fw-semibold mb-1">核心摘要（业务可读）</div>';
                    html += '<div class="small text-muted mb-1">覆盖度：' + escapeHtml(String(cov.level || 'low')) + '（' + escapeHtml(String(cov.hit || 0)) + '/' + escapeHtml(String(cov.total || 0)) + '）</div>';
                    if (pos.length) html += '<div class="mb-1"><span class="fw-semibold">产品定位：</span>' + escapeHtml(pos.join('；')) + '</div>';
                    if (roles.length) html += '<div class="mb-1"><span class="fw-semibold">核心角色：</span>' + escapeHtml(roles.join('；')) + '</div>';
                    if (pay.length) html += '<div class="mb-1"><span class="fw-semibold">付费模型：</span>' + escapeHtml(pay.join('；')) + '</div>';
                    if (proc.length) html += '<div class="mb-1"><span class="fw-semibold">核心流程：</span>' + escapeHtml(proc.join('；')) + '</div>';
                    if (integ.length) html += '<div class="mb-1"><span class="fw-semibold">接口交互：</span>' + escapeHtml(integ.join('；')) + '</div>';
                    if (expt.length) html += '<div class="mb-1"><span class="fw-semibold">异常处理：</span>' + escapeHtml(expt.join('；')) + '</div>';
                    if (impl.length) html += '<div class="mb-1"><span class="fw-semibold">实施计划：</span>' + escapeHtml(impl.join('；')) + '</div>';
                    if (risks.length) html += '<div class="mb-1"><span class="fw-semibold">风险项：</span>' + escapeHtml(risks.join('；')) + '</div>';
                    if (todo.length) html += '<div class="mb-2"><span class="fw-semibold">待确认：</span>' + escapeHtml(todo.join('；')) + '</div>';
                }
            }
            var chain = Array.isArray(ruleModel.priority_chain) ? ruleModel.priority_chain : [];
            if (chain.length) {
                html += '<div class="mb-2"><span class="fw-semibold">优先级链：</span>' + escapeHtml(chain.join(' > ')) + '</div>';
            }
            if (l0) {
                html += '<div class="mb-2"><span class="fw-semibold">L0 一句话：</span>' + escapeHtml(l0) + '</div>';
            }
            if (l1.length) {
                html += '<div class="fw-semibold mb-1">L1 核心运作</div><ol class="mb-2">';
                l1.slice(0, 6).forEach(function(x){ html += '<li>' + escapeHtml(String(x || '')) + '</li>'; });
                html += '</ol>';
            }
            if (l2.length) {
                html += '<div class="fw-semibold mb-1">L2 能力模块</div><div class="mb-2">';
                l2.slice(0, 8).forEach(function(x){ html += '<span class="badge bg-light text-dark border me-1 mb-1">' + escapeHtml(String(x || '')) + '</span>'; });
                html += '</div>';
            }
            if (l3.length) {
                html += '<div class="fw-semibold mb-1">L3 全局规则（已去重）</div><ul class="mb-2">';
                l3.slice(0, 10).forEach(function(x){ html += '<li>' + escapeHtml(String(x || '')) + '</li>'; });
                html += '</ul>';
            }
            if (systemType === 'scheduling_system') {
                var diffRows = Array.isArray(systemModel.module_diff_rules) ? systemModel.module_diff_rules : [];
                if (diffRows.length) {
                    html += '<div class="fw-semibold mb-1">L4 模块差异规则</div>';
                    html += '<div class="table-responsive mb-2"><table class="table table-sm table-bordered extra-table mb-0">';
                    html += '<thead><tr><th style="width:160px;">模块</th><th>差异规则</th></tr></thead><tbody>';
                    diffRows.slice(0, 10).forEach(function(it){
                        var moduleName = String((it && it.module) || '-');
                        var rs = Array.isArray(it && it.rules) ? it.rules : [];
                        html += '<tr><td>' + escapeHtml(moduleName) + '</td><td>' + escapeHtml(rs.join('；') || '-') + '</td></tr>';
                    });
                    html += '</tbody></table></div>';
                } else if (l4.length) {
                    html += '<div class="fw-semibold mb-1">L4 模块差异规则</div><ul class="mb-2">';
                    l4.slice(0, 10).forEach(function(x){ html += '<li>' + escapeHtml(String(x || '')) + '</li>'; });
                    html += '</ul>';
                }
            }
            if (atomicRules.length) {
                html += '<div class="fw-semibold mb-1">规则原子化（高精度）</div>';
                html += '<div class="table-responsive mb-2"><table class="table table-sm table-bordered extra-table mb-0">';
                html += '<thead><tr><th style="width:110px;">规则桶</th><th style="width:180px;">条件</th><th style="width:120px;">对象</th><th>动作</th><th style="width:90px;">置信</th></tr></thead><tbody>';
                atomicRules.slice(0, 12).forEach(function(r){
                    html += '<tr>'
                        + '<td>' + escapeHtml(String(r.bucket || '-')) + '</td>'
                        + '<td>' + escapeHtml(String(r.condition || '-')) + '</td>'
                        + '<td>' + escapeHtml(String(r.actor || '-')) + '</td>'
                        + '<td>' + escapeHtml(String(r.action || '-')) + '</td>'
                        + '<td>' + escapeHtml(String(r.confidence || '-')) + '</td>'
                        + '</tr>';
                });
                html += '</tbody></table></div>';
            }
            if (stateMachine && typeof stateMachine === 'object') {
                var st = Array.isArray(stateMachine.states) ? stateMachine.states : [];
                var tr = Array.isArray(stateMachine.transitions) ? stateMachine.transitions : [];
                var ga = (stateMachine.graph_analysis && typeof stateMachine.graph_analysis === 'object') ? stateMachine.graph_analysis : {};
                if (st.length || tr.length) {
                    html += '<div class="fw-semibold mb-1">状态机建模</div>';
                }
                if (st.length) {
                    html += '<div class="mb-2"><span class="fw-semibold">状态集合：</span>' + escapeHtml(st.join(' → ')) + '</div>';
                }
                if (tr.length) {
                    html += '<div class="table-responsive mb-2"><table class="table table-sm table-bordered extra-table mb-0">';
                    html += '<thead><tr><th style="width:120px;">当前状态</th><th style="width:120px;">触发条件</th><th style="width:120px;">目标状态</th><th>动作</th></tr></thead><tbody>';
                    tr.slice(0, 12).forEach(function(t){
                        html += '<tr><td>' + escapeHtml(String(t.from || '-')) + '</td><td>' + escapeHtml(String(t.trigger || '-')) + '</td><td>' + escapeHtml(String(t.to || '-')) + '</td><td>' + escapeHtml(String(t.action || '-')) + '</td></tr>';
                    });
                    html += '</tbody></table></div>';
                }
                if (stateMermaidCode) {
                    html += '<div class="fw-semibold mb-1 small">状态迁移图</div><div id="outlineStateMachineMermaid" class="mb-2"></div>';
                }
                var deadStates = Array.isArray(ga.dead_end_states) ? ga.dead_end_states : [];
                var unreachableStates = Array.isArray(ga.unreachable_states) ? ga.unreachable_states : [];
                var cycleRows = Array.isArray(ga.cycles) ? ga.cycles : [];
                if (deadStates.length || unreachableStates.length || cycleRows.length) {
                    html += '<div class="fw-semibold mb-1">图分析（死锁/不可达/死胡同）</div>';
                    html += '<div class="small text-muted mb-1">风险等级：' + escapeHtml(String(ga.risk_level || 'low')) + '</div>';
                    if (unreachableStates.length) {
                        html += '<div class="mb-1"><span class="fw-semibold">不可达状态：</span>' + escapeHtml(unreachableStates.join('、')) + '</div>';
                    }
                    if (deadStates.length) {
                        html += '<div class="mb-1"><span class="fw-semibold">无出口状态：</span>' + escapeHtml(deadStates.join('、')) + '</div>';
                    }
                    if (cycleRows.length) {
                        html += '<ul class="mb-2">';
                        cycleRows.slice(0, 6).forEach(function(cy){
                            var seq = Array.isArray(cy) ? cy.join(' -> ') : String(cy || '-');
                            html += '<li>' + escapeHtml(seq) + '</li>';
                        });
                        html += '</ul>';
                    }
                }
            }
            if (ruleDiagnostics && typeof ruleDiagnostics === 'object') {
                var conflicts = Array.isArray(ruleDiagnostics.conflicts) ? ruleDiagnostics.conflicts : [];
                var checks = Array.isArray(ruleDiagnostics.closure_checks) ? ruleDiagnostics.closure_checks : [];
                var summary = (ruleDiagnostics.summary && typeof ruleDiagnostics.summary === 'object') ? ruleDiagnostics.summary : {};
                if (conflicts.length || checks.length) {
                    html += '<div class="fw-semibold mb-1">规则诊断（冲突/闭环）</div>';
                    html += '<div class="small text-muted mb-1">健康度：' + escapeHtml(String(summary.health_level || 'good')) + '｜冲突数：' + escapeHtml(String(summary.conflict_count || 0)) + '｜告警数：' + escapeHtml(String(summary.warn_count || 0)) + '</div>';
                }
                if (conflicts.length) {
                    html += '<ul class="mb-2">';
                    conflicts.slice(0, 6).forEach(function(c){
                        html += '<li>' + escapeHtml(String(c.message || '规则冲突')) + '（' + escapeHtml(String(c.evidence || '-')) + '）</li>';
                    });
                    html += '</ul>';
                }
                if (checks.length) {
                    html += '<div class="table-responsive mb-2"><table class="table table-sm table-bordered extra-table mb-0">';
                    html += '<thead><tr><th style="width:180px;">检查项</th><th style="width:90px;">状态</th><th>说明</th></tr></thead><tbody>';
                    checks.slice(0, 8).forEach(function(c){
                        html += '<tr><td>' + escapeHtml(String(c.name || '-')) + '</td><td>' + escapeHtml(String(c.status || '-')) + '</td><td>' + escapeHtml(String(c.message || '-')) + '</td></tr>';
                    });
                    html += '</tbody></table></div>';
                }
                if (remediationPlan.length) {
                    html += '<div class="fw-semibold mb-1">自动修复建议序列</div>';
                    html += '<div class="table-responsive mb-2"><table class="table table-sm table-bordered extra-table mb-0">';
                    html += '<thead><tr><th style="width:60px;">序号</th><th style="width:70px;">优先级</th><th style="width:150px;">目标</th><th>建议动作</th><th style="width:170px;">影响维度</th><th style="width:90px;">预估提升</th><th style="width:180px;">预期收益</th></tr></thead><tbody>';
                    remediationPlan.slice(0, 8).forEach(function(p){
                        var dims = Array.isArray(p.dimensions) ? p.dimensions.join('、') : '-';
                        html += '<tr><td>' + escapeHtml(String(p.index || '-')) + '</td><td>' + escapeHtml(String(p.priority || '-')) + '</td><td>' + escapeHtml(String(p.target || '-')) + '</td><td>' + escapeHtml(String(p.action || '-')) + '</td><td>' + escapeHtml(String(dims || '-')) + '</td><td>+' + escapeHtml(String(p.score_gain || '0')) + '</td><td>' + escapeHtml(String(p.expected_gain || '-')) + '</td></tr>';
                    });
                    html += '</tbody></table></div>';
                }
            }
            if (deterministicRules && typeof deterministicRules === 'object') {
                var checksDet = Array.isArray(deterministicRules.checks) ? deterministicRules.checks : [];
                var scoreDet = Number(deterministicRules.score || 0);
                if (checksDet.length) {
                    html += '<div class="fw-semibold mb-1">规则引擎裁决（确定性）</div>';
                    html += '<div class="small text-muted mb-1">规则分：' + escapeHtml(String(scoreDet)) + '/100</div>';
                    if (rulePlugin && typeof rulePlugin === 'object') {
                        html += '<div class="small text-muted mb-1">插件：' + escapeHtml(String(rulePlugin.name || rulePlugin.plugin_id || '-')) + '（启用' + escapeHtml(String(rulePlugin.enabled_rule_count || checksDet.length)) + '/' + escapeHtml(String(rulePlugin.total_rule_count || checksDet.length)) + '）</div>';
                        var kh = Array.isArray(rulePlugin.knowledge_hits) ? rulePlugin.knowledge_hits : [];
                        if (kh.length) {
                            html += '<div class="small text-muted mb-1">知识库命中：' + escapeHtml(kh.map(function(x){ return String((x && x.name) || '-'); }).join('、')) + '</div>';
                        }
                    }
                    if (promptProfile && typeof promptProfile === 'object') {
                        html += '<div class="small text-muted mb-1">Prompt Profile：' + escapeHtml(String(promptProfile.name || promptProfile.profile_id || '-')) + '｜版本：' + escapeHtml(String(promptProfile.version || '-')) + '｜A/B：' + escapeHtml(String(promptProfile.variant || 'A')) + '</div>';
                    }
                    if (promptEvaluation && typeof promptEvaluation === 'object') {
                        html += '<div class="small text-muted mb-1">Prompt评估：' + escapeHtml(String(promptEvaluation.quality_estimate || 0)) + '（' + escapeHtml(String(promptEvaluation.passed ? 'PASS' : 'REVIEW')) + '）</div>';
                    }
                    html += '<div class="table-responsive mb-2"><table class="table table-sm table-bordered extra-table mb-0">';
                    html += '<thead><tr><th style="width:70px;">规则ID</th><th style="width:120px;">分类</th><th style="width:130px;">检查类型</th><th style="width:150px;">规则名</th><th style="width:70px;">级别</th><th style="width:70px;">结果</th><th>说明</th></tr></thead><tbody>';
                    checksDet.slice(0, 12).forEach(function(c){
                        html += '<tr><td>' + escapeHtml(String(c.rule_id || '-')) + '</td><td>' + escapeHtml(String(c.category || '-')) + '</td><td>' + escapeHtml(String(c.check_type || '-')) + '</td><td>' + escapeHtml(String(c.name || '-')) + '</td><td>' + escapeHtml(String(c.severity || '-')) + '</td><td>' + escapeHtml(String(c.passed ? 'PASS' : 'FAIL')) + '</td><td>' + escapeHtml(String(c.description || '-')) + '</td></tr>';
                    });
                    html += '</tbody></table></div>';
                }
            }
            if (explainableReport && typeof explainableReport === 'object') {
                var explainItems = Array.isArray(explainableReport.items) ? explainableReport.items : [];
                var explainSummary = (explainableReport.summary && typeof explainableReport.summary === 'object') ? explainableReport.summary : {};
                if (explainItems.length) {
                    html += '<div class="fw-semibold mb-1">冲突自动解释（可读报告）</div>';
                    html += '<div class="small text-muted mb-1">冲突数：' + escapeHtml(String(explainSummary.conflict_count || explainItems.length)) + '｜风险等级：' + escapeHtml(String(explainSummary.risk_level || 'medium')) + '</div>';
                    explainItems.slice(0, 6).forEach(function(it, idx){
                        var cid = String(it.conflict_id || ('C' + String(idx + 1)));
                        var rid = 'outlineExplainMermaid_' + String(idx);
                        html += '<div class="border rounded p-2 mb-2">';
                        html += '<div class="fw-semibold mb-1">' + escapeHtml(cid + '｜' + String(it.type || '规则冲突')) + '</div>';
                        html += '<div class="small mb-1"><span class="text-muted">涉及节点：</span>' + escapeHtml((Array.isArray(it.involved_nodes) ? it.involved_nodes : []).join('、') || '-') + '</div>';
                        html += '<div class="small mb-1"><span class="text-muted">根因：</span>' + escapeHtml(String(it.root_cause || '-')) + '</div>';
                        var impacts = Array.isArray(it.impact) ? it.impact : [];
                        if (impacts.length) html += '<div class="small mb-1"><span class="text-muted">后果：</span>' + escapeHtml(impacts.join('；')) + '</div>';
                        var sugg = Array.isArray(it.suggestion) ? it.suggestion : [];
                        if (sugg.length) html += '<div class="small mb-1"><span class="text-muted">建议：</span>' + escapeHtml(sugg.join('；')) + '</div>';
                        if (String(it.mermaid || '').trim()) {
                            html += '<div class="fw-semibold small mb-1">冲突路径</div><div id="' + rid + '" class="mb-1"></div>';
                            explainMermaidBlocks.push({ id: rid, code: String(it.mermaid || '') });
                        }
                        html += '</div>';
                    });
                }
            }
            if (strategyReport && typeof strategyReport === 'object') {
                var strategySummary = (strategyReport.summary && typeof strategyReport.summary === 'object') ? strategyReport.summary : {};
                var strategyPlans = Array.isArray(strategyReport.plans) ? strategyReport.plans : [];
                if (strategyPlans.length) {
                    html += '<div class="fw-semibold mb-1">修复策略与改造方案</div>';
                    html += '<div class="small text-muted mb-1">方案数：' + escapeHtml(String(strategySummary.plan_count || strategyPlans.length)) + '｜改造优先级：' + escapeHtml(String(strategySummary.priority || 'P1')) + '</div>';
                    strategyPlans.slice(0, 6).forEach(function(p){
                        var module = (p.architecture && p.architecture.module) ? String(p.architecture.module) : '-';
                        var comps = (p.architecture && Array.isArray(p.architecture.components)) ? p.architecture.components : [];
                        var sts = Array.isArray(p.strategies) ? p.strategies : [];
                        var tasks = Array.isArray(p.tasks) ? p.tasks : [];
                        var focus = Array.isArray(p.test_focus) ? p.test_focus : [];
                        html += '<div class="border rounded p-2 mb-2">';
                        html += '<div class="fw-semibold mb-1">' + escapeHtml(String(p.problem_type || '问题类型') + '｜' + String(p.root_type || '-')) + '</div>';
                        html += '<div class="small mb-1"><span class="text-muted">架构模块：</span>' + escapeHtml(module) + (comps.length ? '（' + escapeHtml(comps.join('、')) + '）' : '') + '</div>';
                        if (sts.length) html += '<div class="small mb-1"><span class="text-muted">策略：</span>' + escapeHtml(sts.join('；')) + '</div>';
                        if (tasks.length) html += '<div class="small mb-1"><span class="text-muted">落地任务：</span>' + escapeHtml(tasks.join('；')) + '</div>';
                        if (focus.length) html += '<div class="small mb-1"><span class="text-muted">测试关注：</span>' + escapeHtml(focus.join('；')) + '</div>';
                        html += '</div>';
                    });
                }
            }
        }
        if (outline.length) {
            html += '<div class="table-responsive"><table class="table table-sm table-bordered extra-table mb-0">';
            html += '<thead><tr><th style="width:70px;">序号</th><th style="width:260px;">一级模块</th><th>二级要点</th></tr></thead><tbody>';
            outline.forEach(function(it) {
                var children = Array.isArray(it.children) ? it.children : [];
                html += '<tr><td>' + escapeHtml(String(it.index || '')) + '</td><td>' + escapeHtml(it.title || '') + '</td><td>' + escapeHtml(children.join('；') || '-') + '</td></tr>';
            });
            html += '</tbody></table></div>';
        } else {
            html += '<ol class="mb-0 ps-3">';
            rows.forEach(function(r) {
                var indent = Math.max(0, Number(r.level || 1) - 1) * 18;
                html += '<li style="margin-left:' + indent + 'px;">' + escapeHtml(r.title || '') + '</li>';
            });
            html += '</ol>';
        }
        var q = outlineEngine && outlineEngine.quality_score ? outlineEngine.quality_score : {};
        var meta = '模式：' + (outlineEngine && outlineEngine.mode ? outlineEngine.mode : 'structured');
        meta += '｜系统类型：' + systemType;
        if (classifierConfidence > 0) meta += '｜识别置信：' + classifierConfidence.toFixed(2);
        if (q && q.overall != null) meta += '｜结构质量分：' + String(q.overall);
        contentOutlineMainMetaEl.textContent = meta;
        contentOutlineMainContentEl.innerHTML = html;
        bindOwnerCorrectionActions();
        if (stateMermaidCode) {
            renderMermaidBlock(stateMermaidCode, 'outlineStateMachineMermaid');
        }
        explainMermaidBlocks.forEach(function(b){
            renderMermaidBlock(String(b.code || ''), String(b.id || ''));
        });
        contentOutlineMainWrapEl.style.display = 'block';
    }

    function buildOutlineReportMarkdown() {
        var lines = ['# 需求认知拉齐稿（评审会版）', ''];
        
        var ruleModel = (outlineEngine && outlineEngine.rule_model && typeof outlineEngine.rule_model === 'object') ? outlineEngine.rule_model : {};
        var explicitOutline = (outlineEngine && outlineEngine.explicit_outline && typeof outlineEngine.explicit_outline === 'object') ? outlineEngine.explicit_outline : {};
        var systemModel = (outlineEngine && outlineEngine.system_model && typeof outlineEngine.system_model === 'object') ? outlineEngine.system_model : {};
        if (!explicitOutline || Object.keys(explicitOutline).length === 0) {
            explicitOutline = (systemModel && systemModel.explicit_outline && typeof systemModel.explicit_outline === 'object') ? systemModel.explicit_outline : {};
        }

        var eBizMd = Array.isArray(explicitOutline.business_summary) ? explicitOutline.business_summary : [];
        var eRolesMd = Array.isArray(explicitOutline.roles) ? explicitOutline.roles : [];
        var eFlowMd = Array.isArray(explicitOutline.main_flow) ? explicitOutline.main_flow : [];
        var ePendingMd = Array.isArray(explicitOutline.pending_list) ? explicitOutline.pending_list : [];
        
        lines.push('## 一、系统目标');
        if (eBizMd.length) {
            lines.push(eBizMd.join('；'));
        } else {
            lines.push('根据业务优先级动态展示内容，并处理各功能之间打断、恢复、切换逻辑。');
        }
        lines.push('');
        
        lines.push('## 二、本期范围');
        if (eRolesMd.length) {
            eRolesMd.forEach(function(r) { lines.push('* ' + String(r)); });
        } else {
            lines.push('* 暂无识别范围');
        }
        lines.push('');
        
        lines.push('## 三、统一优先级（需确认）');
        lines.push('```text');
        if (Array.isArray(ruleModel.priority_chain) && ruleModel.priority_chain.length) {
            lines.push(ruleModel.priority_chain.join(' > '));
        } else {
            lines.push('暂无识别的优先级');
        }
        lines.push('```');
        lines.push('');
        
        lines.push('## 四、核心流程');
        if (eFlowMd.length) {
            eFlowMd.forEach(function(f) { lines.push(String(f)); });
        } else {
            lines.push('### 正常流程');
            lines.push('暂无识别流程');
        }
        lines.push('');
        
        lines.push('## 五、本次会议待拍板问题（最重要）');
        if (ePendingMd.length) {
            ePendingMd.forEach(function(p, i) { lines.push(String(i+1) + '. ' + String(p)); });
        } else {
            lines.push('1. 暂无待拍板问题');
        }
        lines.push('');
        
        lines.push('## 六、当前风险');
        lines.push('若不定规则直接开发：');
        lines.push('* 前后端理解不一致');
        lines.push('* 测试无法验收');
        lines.push('* 联调返工高');
        lines.push('* 上线后展示混乱');
        lines.push('');
        
        lines.push('## 七、输出物');
        lines.push('会议结束后补齐：');
        lines.push('* 状态机图');
        lines.push('* 优先级规则表');
        lines.push('* 异常流程表');
        lines.push('* 验收标准');
        lines.push('');
        
        return lines.join('\n');
    }

    function renderKnowledgeCards() {
        if (!knowledgeEl) return;
        if (!knowledgeCards.length) {
            knowledgeEl.innerHTML = '<div class="text-muted small">暂无能力卡片</div>';
            return;
        }
        var html = '<div class="table-responsive mb-2"><table class="table table-sm table-striped extra-table mb-0">';
        html += '<thead><tr><th style="width:130px;">能力ID</th><th style="width:480px;">需求名</th><th style="width:90px;">优先级</th><th style="width:520px;">领域</th><th style="width:140px;">操作</th></tr></thead><tbody>';
        knowledgeCards.forEach(function(card, idx) {
            html += '<tr>';
            html += '<td>' + escapeHtml(card.capability_id || '-') + '</td>';
            html += '<td>' + escapeHtml(card.name || '-') + '</td>';
            html += '<td>' + escapeHtml(card.priority || '-') + '</td>';
            html += '<td>' + escapeHtml(card.domain || '-') + '</td>';
            html += '<td>' +
                '<button class="btn btn-sm btn-outline-primary me-1 btn-knowledge-edit" data-idx="' + idx + '">编辑</button>' +
                '<button class="btn btn-sm btn-outline-danger btn-knowledge-delete" data-idx="' + idx + '">删除</button>' +
                '</td>';
            html += '</tr>';
        });
        html += '</tbody></table></div>';
        html += '<div class="text-muted small mb-2">下方为详细内容预览</div>';
        knowledgeCards.forEach(function(card) {
            var pre = Array.isArray(card.preconditions) ? card.preconditions.join('；') : '';
            var behavior = Array.isArray(card.system_behaviors) ? card.system_behaviors.join('；') : '';
            var exceptions = Array.isArray(card.exceptions) ? card.exceptions.map(function(it){
                return (it.condition || '') + '：' + (it.action || '');
            }).join('；') : '';
            var logs = Array.isArray(card.logging) ? card.logging.join('、') : '';
            var ac = Array.isArray(card.acceptance_criteria) ? card.acceptance_criteria.join('；') : '';
            html += '<div class="table-responsive mb-3"><table class="table table-sm table-bordered extra-table mb-0">';
            html += '<tbody>';
            html += '<tr><th style="width:120px;">能力ID</th><td>' + escapeHtml(card.capability_id || '-') + '</td></tr>';
            html += '<tr><th>需求名</th><td>' + escapeHtml(card.name || '-') + '</td></tr>';
            html += '<tr><th>领域</th><td>' + escapeHtml(card.domain || '-') + '</td></tr>';
            html += '<tr><th>类别</th><td>' + escapeHtml(card.category || '-') + '</td></tr>';
            html += '<tr><th>优先级</th><td>' + escapeHtml(card.priority || '-') + '</td></tr>';
            html += '<tr><th>触发</th><td>' + escapeHtml(card.trigger || '-') + '</td></tr>';
            html += '<tr><th>前置条件</th><td>' + escapeHtml(pre || '-') + '</td></tr>';
            html += '<tr><th>系统行为</th><td>' + escapeHtml(behavior || '-') + '</td></tr>';
            html += '<tr><th>异常处理</th><td>' + escapeHtml(exceptions || '-') + '</td></tr>';
            html += '<tr><th>埋点日志</th><td>' + escapeHtml(logs || '-') + '</td></tr>';
            html += '<tr><th>验收标准</th><td>' + escapeHtml(ac || '-') + '</td></tr>';
            html += '</tbody></table></div>';
        });
        knowledgeEl.innerHTML = html;
    }

    function getKnowledgeTemplate() {
        return {
            capability_id: 'KTV_NEW_' + Date.now(),
            name: '新能力',
            domain: 'KTV点歌系统',
            category: '播放控制',
            priority: 'P1',
            trigger: '',
            preconditions: [],
            system_behaviors: [],
            exceptions: [],
            logging: [],
            acceptance_criteria: []
        };
    }

    function openKnowledgeCardEditor(card, idx) {
        if (!knowledgeCardModal || !knowledgeCardJson) return;
        editingKnowledgeIndex = Number(idx);
        if (knowledgeCardModalTitle) {
            knowledgeCardModalTitle.textContent = (editingKnowledgeIndex >= 0 ? '编辑能力卡片' : '新增能力卡片');
        }
        if (knowledgeCardStatus) knowledgeCardStatus.textContent = '';
        knowledgeCardJson.value = JSON.stringify(card || getKnowledgeTemplate(), null, 2);
        knowledgeCardModal.show();
    }

    function loadKnowledgeCards() {
        return apiJson(baseUrl + '/api/knowledge_cards?domain=all', { method: 'GET' })
            .then(function(data) {
                knowledgeCards = Array.isArray(data.items) ? data.items : [];
                renderKnowledgeCards();
                renderExtras();
            })
            .catch(function(err) {
                knowledgeCards = [];
                if (knowledgeEl) {
                    knowledgeEl.innerHTML = '<div class="text-danger small">加载能力卡片失败：' + escapeHtml(err.message || String(err)) + '</div>';
                }
                renderExtras();
            });
    }

    function saveKnowledgeCards() {
        return apiJson(baseUrl + '/api/knowledge_cards', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ items: knowledgeCards })
        }).then(function(data) {
            knowledgeCards = Array.isArray(data.items) ? data.items : knowledgeCards;
            renderKnowledgeCards();
            renderExtras();
            return data;
        });
    }

    function renderAuditDashboard(summary, defects) {
        if (!dashboardEl || !summary) return;
        dashboardEl.style.display = 'block';
        
        var score = (summary.quality_score != null) ? Number(summary.quality_score).toFixed(1) : '0.0';
        document.getElementById('overviewScore').textContent = score;
        
        // 更新环形进度条
        var scoreVal = parseFloat(score);
        var progressEl = document.getElementById('gaugeProgress');
        if (progressEl) {
            var circumference = 2 * Math.PI * 45;
            var offset = circumference - (scoreVal / 10) * circumference;
            progressEl.style.strokeDashoffset = offset;
            
            // 根据分数改变颜色
            if (scoreVal >= 8) progressEl.style.stroke = '#198754';
            else if (scoreVal >= 6) progressEl.style.stroke = '#1a73e8';
            else if (scoreVal >= 4) progressEl.style.stroke = '#fd7e14';
            else progressEl.style.stroke = '#dc3545';
        }

        // 计算 P0/P1/P2
        var p0 = 0, p1 = 0, p2 = 0;
        var counts = { all: 0, state: 0, flow: 0, auth: 0, other: 0 };
        
        (defects || []).forEach(function(d) {
            var lv = String(d.risk_level || '').toUpperCase();
            if (lv === 'P0') p0++;
            else if (lv === 'P1') p1++;
            else p2++;
            
            counts.all++;
            var type = String(d.type || '') + String(d.description || '');
            if (type.indexOf('状态') >= 0) counts.state++;
            else if (type.indexOf('流程') >= 0 || type.indexOf('并发') >= 0) counts.flow++;
            else if (type.indexOf('权限') >= 0 || type.indexOf('安全') >= 0) counts.auth++;
            else counts.other++;
        });
        
        document.getElementById('statP0').textContent = p0;
        document.getElementById('statP1').textContent = p1;
        document.getElementById('statP2').textContent = p2;
        document.getElementById('overviewTitle').textContent = '总览 (' + (p0+p1+p2) + ')';
        
        // 更新分类 Filter 数字
        if (document.getElementById('countAll')) document.getElementById('countAll').textContent = counts.all;
        if (document.getElementById('countState')) document.getElementById('countState').textContent = counts.state;
        if (document.getElementById('countFlow')) document.getElementById('countFlow').textContent = counts.flow;
        if (document.getElementById('countAuth')) document.getElementById('countAuth').textContent = counts.auth;
        if (document.getElementById('countOther')) document.getElementById('countOther').textContent = counts.other;
        
        if (document.getElementById('categoryFilters')) document.getElementById('categoryFilters').style.display = 'flex';
        renderStage2ScanMeta(scanMetaData);
    }

    /** 历史快照等场景下从缺陷列表粗算总览分（与后端算法近似，仅用于展示） */
    function buildMinimalSummaryFromDefects(defects) {
        var p0 = 0, p1 = 0, p2 = 0;
        (defects || []).forEach(function(d) {
            var lv = String(d.risk_level || '').toUpperCase();
            if (lv === 'P0') p0++;
            else if (lv === 'P1') p1++;
            else p2++;
        });
        var total = p0 + p1 + p2;
        var score = total === 0 ? 8.5 : Math.max(0, Math.min(10, 10 - (p0 * 2.5 + p1 * 1.2 + p2 * 0.4)));
        score = Math.round(score * 10) / 10;
        var mp = (defects && defects[0] && defects[0].description) ? String(defects[0].description) : '未发现明显漏洞';
        return {
            quality_score: score,
            risk_level: p0 > 0 ? 'P0' : (p1 > 0 ? 'P1' : 'P2'),
            main_problem: mp
        };
    }

    function renderStage2ScanMeta(meta) {
        var row = document.getElementById('stage2ScanMetaRow');
        var badge = document.getElementById('stage2ScanBadge');
        var detail = document.getElementById('stage2ScanDetail');
        if (!row || !badge || !detail) return;
        if (!meta || typeof meta !== 'object' || Object.keys(meta).length === 0) {
            row.style.display = 'none';
            return;
        }
        var ok = meta.llm_scan_ok !== false;
        badge.className = 'badge ' + (ok ? 'bg-success' : 'bg-danger');
        badge.textContent = ok ? '正常' : '失败';
        var parts = [];
        if (meta.llm_defects_parsed != null) parts.push('LLM 解析 ' + meta.llm_defects_parsed + ' 条');
        if (meta.rule_defects_count != null) parts.push('规则库 ' + meta.rule_defects_count + ' 条');
        detail.textContent = '';
        detail.removeAttribute('title');
        if (!ok && meta.llm_error) {
            var err = String(meta.llm_error);
            if (err.length > 160) err = err.slice(0, 160) + '…';
            detail.textContent = err;
            detail.className = 'text-danger small';
            detail.setAttribute('title', err);
        } else {
            detail.textContent = parts.join(' · ');
            detail.className = 'text-muted small';
        }
        row.style.display = 'block';
    }

    function renderDefectCards(defects, filterCategory) {
        if (!cardAreaEl) return;
        
        var filtered = defects || [];
        if (filterCategory && filterCategory !== 'all') {
            filtered = filtered.filter(function(d) {
                var type = String(d.type || '') + String(d.description || '');
                if (filterCategory === '状态机') return type.indexOf('状态') >= 0;
                if (filterCategory === '流程') return type.indexOf('流程') >= 0 || type.indexOf('并发') >= 0;
                if (filterCategory === '权限') return type.indexOf('权限') >= 0 || type.indexOf('安全') >= 0;
                if (filterCategory === '其他') return type.indexOf('状态') < 0 && type.indexOf('流程') < 0 && type.indexOf('并发') < 0 && type.indexOf('权限') < 0 && type.indexOf('安全') < 0;
                return true;
            });
        }

        if (!filtered.length) {
            cardAreaEl.innerHTML = '<div class="text-muted p-5 text-center">该分类下未发现明显漏洞</div>';
            return;
        }
        
        // 按风险等级排序 P0 -> P1 -> P2
        var sorted = filtered.slice().sort(function(a, b) {
            var levels = { 'P0': 0, 'P1': 1, 'P2': 2 };
            return (levels[String(a.risk_level).toUpperCase()] || 9) - (levels[String(b.risk_level).toUpperCase()] || 9);
        });
        
        var html = '';
        sorted.forEach(function(d) {
            var lv = String(d.risk_level || 'P2').toUpperCase();
            var lvClass = lv.toLowerCase();
            
            html += '<div class="defect-card ' + lvClass + '">';
            html += '  <div class="defect-header">';
            html += '    <div class="defect-title-box">';
            html += '      <span class="defect-level-badge badge-' + lvClass + '">' + lv + '</span>';
            html += '      <span class="defect-name">' + escapeHtml(d.type || '分析项') + '</span>';
            html += '    </div>';
            html += '    <div class="defect-tags">';
            html += '      <span class="defect-tag">' + escapeHtml(d.module || '全局') + '</span>';
            if (d.anchor) html += '      <span class="defect-tag">' + escapeHtml(d.anchor) + '</span>';
            html += '    </div>';
            html += '  </div>';
            
            html += '  <div class="defect-info-row">';
            html += '    <div class="defect-info-label">描述</div>';
            html += '    <div class="defect-info-content">' + escapeHtml(d.description || '-') + '</div>';
            html += '  </div>';
            
            html += '  <div class="defect-info-row">';
            html += '    <div class="defect-info-label">原因</div>';
            html += '    <div class="defect-info-content">' + escapeHtml(d.reason || '-') + '</div>';
            html += '  </div>';
            
            html += '  <div class="defect-info-row">';
            html += '    <div class="defect-info-label">建议</div>';
            html += '    <div class="defect-info-content">' + escapeHtml(d.suggestion || '-') + '</div>';
            html += '  </div>';
            
            html += '  <div class="defect-footer">';
            var btnText = '补充' + (d.type || '说明');
            html += '    <button class="action-btn">' + escapeHtml(btnText) + ' <i class="fas fa-chevron-down ms-1"></i></button>';
            html += '  </div>';
            html += '</div>';
        });
        cardAreaEl.innerHTML = html;
        cardAreaEl.style.display = 'block';
    }

    function renderParse() {
        if (!parseEl) return;
        if (!parseMeta || typeof parseMeta !== 'object') {
            parseEl.innerHTML = '<div class="text-muted small">暂无解析质量数据</div>';
            return;
        }
        var pq = parseMeta.parse_quality || {};
        var req = parseMeta.required_elements || {};
        var conflicts = Array.isArray(parseMeta.conflict_candidates) ? parseMeta.conflict_candidates : [];
        var blocks = Array.isArray(parseMeta.blocks) ? parseMeta.blocks : [];

        var html = '';
        if (pq && typeof pq === 'object') {
            html += '<div class="fw-semibold mb-2">解析质量评分</div>';
            html += renderKeyValueTable('', [
                { k: '总体评分', v: (pq.overall != null ? (pq.overall + '/10') : '【PRD未说明】') },
                { k: '章节块数量', v: (pq.blocks != null ? String(pq.blocks) : String(blocks.length || 0)) },
                { k: '提示', v: (Array.isArray(pq.notes) ? pq.notes.join('；') : '') }
            ]);
        }
        if (req && typeof req === 'object') {
            var items = Array.isArray(req.items) ? req.items : [];
            if (items.length) {
                html += '<div class="fw-semibold mb-2">必备要素健康度（Required Elements）</div>';
                html += '<div class="table-responsive"><table class="table table-sm table-striped extra-table mb-3">';
                html += '<thead><tr><th style="min-width:160px;">要素</th><th>是否存在</th><th>数量</th><th>评分</th><th>影响/建议</th></tr></thead><tbody>';
                items.forEach(function(it) {
                    var badge = it.present ? '<span class="badge bg-success">存在</span>' : '<span class="badge bg-danger">缺失</span>';
                    html += '<tr><td>' + escapeHtml(it.name) + '</td><td>' + badge + '</td><td>' + escapeHtml(String(it.count || 0)) + '</td><td>' + escapeHtml(String(it.score || '')) + '</td><td>' + escapeHtml(it.impact || '') + '</td></tr>';
                });
                html += '</tbody></table></div>';
            }
        }
        if (conflicts.length) {
            html += '<div class="fw-semibold mb-2">潜在冲突候选（需澄清口径）</div>';
            html += '<div class="table-responsive"><table class="table table-sm table-striped extra-table mb-3">';
            html += '<thead><tr><th style="min-width:220px;">规则A</th><th style="min-width:220px;">规则B</th><th>原因</th><th>锚点</th></tr></thead><tbody>';
            conflicts.slice(0, 10).forEach(function(c) {
                html += '<tr><td>' + escapeHtml(c.a || '') + '</td><td>' + escapeHtml(c.b || '') + '</td><td>' + escapeHtml(c.reason || '') + '</td><td>' + escapeHtml(c.anchor || '') + '</td></tr>';
            });
            html += '</tbody></table></div>';
        }
        if (blocks.length) {
            html += '<div class="fw-semibold mb-2">章节切块（前 15 块）</div>';
            html += '<div class="table-responsive"><table class="table table-sm table-striped extra-table mb-0">';
            html += '<thead><tr><th>Level</th><th style="min-width:240px;">标题</th><th>范围</th><th>摘要</th></tr></thead><tbody>';
            blocks.slice(0, 15).forEach(function(b) {
                var sum = (b.content || '').slice(0, 80);
                html += '<tr><td>' + escapeHtml(String(b.level || '')) + '</td><td>' + escapeHtml(b.title || '') + '</td><td>' + escapeHtml(b.range || '') + '</td><td>' + escapeHtml(sum) + '</td></tr>';
            });
            html += '</tbody></table></div>';
        }

        parseEl.innerHTML = html || '<div class="text-muted small">暂无解析质量数据</div>';
    }

    function renderExtras() {
        var wrap = document.getElementById('leftExtraWrap') || extraWrap;
        if (!wrap) return;
        // 页面瘦身：本页只保留「内容大纲 + 分层审计报告」。
        // Stage4/5 测试矩阵、系统图、知识卡片、导读等“非大纲/非报告本体”内容不在本页展示。
        wrap.style.display = 'none';
        return;
        var hasFeatureNodes = buildFeatureNodes().length > 0;
        var hasOutline = outlineEngine && typeof outlineEngine === 'object';
        var hasImpact = platformImpact && typeof platformImpact === 'object';
        var hasDependency = dependencyAnalysis && typeof dependencyAnalysis === 'object';
        var hasPrdQuality = prdQuality && typeof prdQuality === 'object';
        var hasTestPoints = testPoints && typeof testPoints === 'object';
        var hasValidationOutline = validationOutline && typeof validationOutline === 'object';
        var hasRiskPrediction = riskPrediction && typeof riskPrediction === 'object';
        var hasUnderstandingCards = understandingCards && typeof understandingCards === 'object';
        var hasReleaseGate = releaseGate && typeof releaseGate === 'object';
        var hasReaderGuide = readerGuide && typeof readerGuide === 'object';
        if ((testMatrix && typeof testMatrix === 'object') || (diagrams && typeof diagrams === 'object') || (kg && typeof kg === 'object') || (parseMeta && typeof parseMeta === 'object') || hasOutline || hasImpact || hasDependency || hasPrdQuality || hasTestPoints || hasValidationOutline || hasRiskPrediction || hasUnderstandingCards || hasReleaseGate || hasReaderGuide || hasFeatureNodes || (knowledgeCards && knowledgeCards.length > 0)) {
            wrap.style.display = 'block';
            renderQualityBadges();
            renderReaderGuideMain();
            renderStage4();
            renderUnderstandingCards();
            renderReleaseGate();
            renderDecisionPanelMain();
            renderTestPoints();
            renderStage5();
            renderKG();
            renderPrdQuality();
            renderRiskPrediction();
            renderOutlineEngine();
            renderPlatformImpact();
            renderDependencyAnalysis();
            renderFeatureMindmap();
            renderMainOutline();
            renderKnowledgeCards();
        } else {
            wrap.style.display = 'none';
            if (readerGuideMainWrapEl) readerGuideMainWrapEl.style.display = 'none';
            if (contentOutlineMainWrapEl) {
                if (llmFourPillarsPayload && llmFourPillarsPayload.llm) {
                    renderLlmFourPillarsBlock();
                } else {
                    contentOutlineMainWrapEl.style.display = 'none';
                }
            }
            renderDecisionPanelMain();
        }
        applyLevelVisibility(reportLevel || 'L3');
    }

    function focusFeatureMapTab() {
        var btn = document.querySelector('button[data-bs-target="#leftPaneFeatureMap"]');
        if (!btn || !window.bootstrap || !window.bootstrap.Tab) return;
        var tab = new window.bootstrap.Tab(btn);
        tab.show();
    }

    function renderQualityBadges() {
        var el = document.getElementById('leftExtrasQualityBadges');
        if (!el) return;
        var q4 = extrasQuality && extrasQuality.stage4 && extrasQuality.stage4.overall;
        var q5 = extrasQuality && extrasQuality.stage5 && extrasQuality.stage5.overall;
        function badge(text, cls) {
            return '<span class="badge ' + cls + ' ms-1" style="font-size:0.65rem;">' + escapeHtml(text) + '</span>';
        }
        var html = '';
        if (q4 != null) {
            html += badge('S4:' + q4, q4 >= 8 ? 'bg-success' : (q4 >= 5 ? 'bg-warning text-dark' : 'bg-danger'));
        }
        if (q5 != null) {
            html += badge('S5:' + q5, q5 >= 8 ? 'bg-success' : (q5 >= 5 ? 'bg-warning text-dark' : 'bg-danger'));
        }
        el.innerHTML = html;
    }

    function showStatus(msg) {
        statusText.textContent = msg || '分析中…';
        statusText.style.display = msg ? 'inline-block' : 'none';
    }
    function showReportButtons() {
        btnCopy.style.display = 'inline-block';
        btnDownloadMd.style.display = 'inline-block';
        btnDownloadWord.style.display = 'inline-block';
        var w = document.getElementById('btnDownloadByLevelWrap');
        if (w) w.style.display = 'inline-block';
        btnSaveCases.style.display = 'inline-block';
    }

    docUpload.addEventListener('change', function(e) {
        var file = e.target.files && e.target.files[0];
        console.log('File selected:', file);
        if (!file) return;
        var form = new FormData();
        form.append('file', file);
        var url = file.name.toLowerCase().endsWith('.docx') ? baseUrl + '/api/parse_docx' : baseUrl + '/api/parse_pdf';
        console.log('Fetch URL:', url);
        showStatus('正在解析 ' + file.name + '…');
        btnGenerate.disabled = true;
        fetch(url, { method: 'POST', body: form })
            .then(function(r) {
                console.log('Response status:', r.status);
                var ct = (r.headers.get('Content-Type') || '');
                if (ct.indexOf('application/json') !== -1) {
                    return r.json().then(function(res) {
                        console.log('JSON result:', res);
                        if (!r.ok) throw new Error((res && res.message) || r.statusText || '请求失败');
                        return res;
                    });
                }
                return r.text().then(function(t) { throw new Error(r.status + ' ' + r.statusText + (t ? ': ' + t.slice(0, 100) : '')); });
            })
            .then(function(res) {
                console.log('Final step: res data exists?', !!(res && res.data));
                if (res && res.data) {
                    var text = (res.data.text != null) ? String(res.data.text) : '';
                    if (res.data.warning) text += '\n\n' + res.data.warning;
                    console.log('Extracted text length:', text.length);
                    
                    // 动态查找一次 inputEl，确保引用最新
                    var targetInput = document.getElementById('inputContent') || inputEl;
                    console.log('targetInput element:', targetInput);

                    if (targetInput) {
                        targetInput.value = text;
                        // 强制改变 DOM 值（某些浏览器特性）
                        targetInput.setAttribute('value', text);
                        // 触发事件
                        var event = new Event('input', { bubbles: true });
                        targetInput.dispatchEvent(event);
                        console.log('Text assigned to textarea');
                    } else {
                        console.error('CRITICAL: inputContent textarea not found in DOM!');
                        alert('错误：无法找到输入框，请刷新页面重试。');
                    }
                    if (!text.trim()) alert('未从文件中提取到文本，请尝试粘贴 PRD 或上传其他文件。');
                } else {
                    alert((res && res.message) || '未返回解析结果');
                }
            })
            .catch(function(err) { 
                console.error('Parse error:', err);
                alert('解析失败: ' + (err.message || String(err))); 
            })
            .finally(function() {
                showStatus('');
                btnGenerate.disabled = false;
                e.target.value = '';
            });
    });

    if (bugCsvUpload) {
        bugCsvUpload.addEventListener('change', function(e) {
            var file = e.target.files && e.target.files[0];
            if (!file) return;
            var form = new FormData();
            form.append('file', file);
            setBugStatus('CSV 导入中...', false);
            fetch(baseUrl + '/api/bug/import', { method: 'POST', body: form })
                .then(function(r){ return r.json(); })
                .then(function(res){
                    if (!res || !res.success) throw new Error((res && res.message) || '导入失败');
                    setBugStatus('CSV 导入成功：' + ((res.data && res.data.imported_count) || 0) + ' 条', false);
                    renderBugResult('Bug 导入结果', res.data || {});
                })
                .catch(function(err){
                    setBugStatus('CSV 导入失败：' + (err.message || String(err)), true);
                })
                .finally(function(){ e.target.value = ''; });
        });
    }

    if (btnBugImport) {
        btnBugImport.addEventListener('click', function() {
            var text = String((bugInputEl && bugInputEl.value) || '').trim();
            if (!text) { setBugStatus('请输入 Bug 文本后再导入', true); return; }
            setBugStatus('Bug 文本导入中...', false);
            apiJson(baseUrl + '/api/bug/import', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ text: text })
            }).then(function(data){
                setBugStatus('导入成功：' + (data.imported_count || 0) + ' 条', false);
                renderBugResult('Bug 导入结果', data || {});
            }).catch(function(err){
                setBugStatus('导入失败：' + (err.message || String(err)), true);
            });
        });
    }

    function doJiraImport(isPreview) {
        var jiraUrl = document.getElementById('jiraUrl').value.trim();
        var username = document.getElementById('jiraUsername').value.trim();
        var token = document.getElementById('jiraToken').value.trim();
        var jql = document.getElementById('jiraJql').value.trim();

        if (!jiraUrl || !username || !token || !jql) {
            setBugStatus('请完整填写 Jira 配置信息（地址、账号、Token、JQL）', true);
            return;
        }

        setBugStatus(isPreview ? 'Jira 预览分析中...' : 'Jira 导入中...', false);
        var btn = isPreview ? btnJiraPreview : btnJiraImport;
        if (btn) btn.disabled = true;

        apiJson(baseUrl + '/api/bug/import/jira', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                jira_url: jiraUrl,
                username: username,
                api_token: token,
                jql: jql,
                preview: isPreview
            })
        }).then(function(data){
            if (isPreview) {
                setBugStatus('预览分析完成', false);
                // 借用原有的 bugResult 渲染预览结果
                renderBugResult('Jira 预审结果', data || {});
            } else {
                setBugStatus('Jira 导入成功：' + (data.imported_count || 0) + ' 条', false);
                renderBugResult('Jira 导入结果', data || {});
            }
        }).catch(function(err){
            setBugStatus((isPreview ? '预览分析失败：' : '导入失败：') + (err.message || String(err)), true);
        }).finally(function() {
            if (btn) btn.disabled = false;
        });
    }

    if (btnJiraPreview) {
        btnJiraPreview.addEventListener('click', function() { doJiraImport(true); });
    }
    if (btnJiraImport) {
        btnJiraImport.addEventListener('click', function() { doJiraImport(false); });
    }

    if (btnBugAnalyze) {
        btnBugAnalyze.addEventListener('click', function() {
            var text = String((bugInputEl && bugInputEl.value) || '').trim();
            if (!text) { setBugStatus('请输入 Bug 文本后再解析', true); return; }
            var bugs = text.split(/\r?\n/).map(function(x){ return x.trim(); }).filter(Boolean);
            setBugStatus('Bug 解析中...', false);
            apiJson(baseUrl + '/api/bug/analyze', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ bugs: bugs })
            }).then(function(data){
                setBugStatus('解析完成：' + ((data.items && data.items.length) || 0) + ' 条', false);
                renderBugResult('Bug 解析结果', data || {});
            }).catch(function(err){
                setBugStatus('解析失败：' + (err.message || String(err)), true);
            });
        });
    }

    if (btnPrdAuditByBug) {
        btnPrdAuditByBug.addEventListener('click', function() {
            var prd = String((inputEl && inputEl.value) || '').trim();
            if (!prd) { setBugStatus('请先输入 PRD 再执行审计', true); return; }
            setBugStatus('按 Bug 模式审计中...', false);
            apiJson(baseUrl + '/api/prd/audit', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ prd_text: prd, use_llm: !!(useLLMCheckbox && useLLMCheckbox.checked) })
            }).then(function(data){
                setBugStatus('审计完成：Bug命中 ' + ((data.bug_hits && data.bug_hits.length) || 0), false);
                renderBugResult('Bug 审计命中', { bug_hits: data.bug_hits || [], vector_hits: data.vector_hits || [] });
                
                // 展示知识库匹配结果（本地规则匹配）
                var knowledgeMatches = data.knowledge_matches || [];
                if (knowledgeMatches.length > 0) {
                    renderKnowledgeMatches(knowledgeMatches);
                }
                
                var audit = data.audit_result || {};
                if (audit && audit.L3) {
                    reports.L1 = audit.L1 || '';
                    reports.L2 = audit.L2 || '';
                    reports.L3 = audit.L3 || '';
                    setLevel(reportLevel || 'L3');
                    renderExtras();
                }
            }).catch(function(err){
                setBugStatus('审计失败：' + (err.message || String(err)), true);
            });
        });
    }

    var levelNames = { OUTLINE: '认知大纲', L1: '管理摘要 (L1)', L2: '产品分析 (L2)', L3: '技术审计 (L3)', VALIDATION: '验证大纲', IMPACT: '变更影响分析' };
    function applyLevelVisibility(level) {
        var isOutline = level === 'OUTLINE';
        var isL1 = level === 'L1';
        var isL2 = level === 'L2';
        var isL3 = level === 'L3';
        var isValidation = level === 'VALIDATION';
        var isImpact = level === 'IMPACT';
        // 主报告区：各页签只显示本层级内容（不混排导读/导图/平台影响/大纲等）
        var map = {
            decisionPanelMain: isL3,
            fourPillarsAuditWrap: isL1 || isL2 || isL3,
            sharedSummaryMainWrap: isOutline,
            contentOutlineMainWrap: isOutline,
            changeImpactMainWrap: isImpact,
            validationOutlineMainWrap: isValidation
        };
        Object.keys(map).forEach(function(id) {
            var el = document.getElementById(id);
            if (!el) return;
            el.style.display = map[id] ? 'block' : 'none';
        });
        // 导读、功能导图、平台影响：不放入任一主栏层级（避免与 L3/L1/L2/大纲混排）；仍由左侧「测试矩阵与系统图」等补充区承载 Stage 产物
        ['readerGuideMainWrap', 'mainFeatureMapWrap', 'platformImpactMainWrap'].forEach(function(id) {
            var el = document.getElementById(id);
            if (el) el.style.display = 'none';
        });
        if (dashboardEl) dashboardEl.style.display = isL3 ? 'block' : 'none';
        var cat = document.getElementById('categoryFilters');
        if (cat) cat.style.display = (isL3 && currentMode === 'card') ? 'flex' : 'none';
        if (levelContextBadgeEl) levelContextBadgeEl.textContent = levelNames[level] || level;
        if (levelContextDescEl) {
            if (isOutline) levelContextDescEl.textContent = '当前展示全员共识摘要与认知大纲；结构视图统一收敛到技术审计 (L3)';
            else if (isL3) levelContextDescEl.textContent = '当前仅展示技术审计（L3）报告与总览';
            else if (isL2) levelContextDescEl.textContent = '当前仅展示产品分析（L2）报告';
            else if (isL1) levelContextDescEl.textContent = '当前仅展示管理摘要（L1）报告';
            else if (isImpact) levelContextDescEl.textContent = '当前仅展示架构级变更影响分析';
            else levelContextDescEl.textContent = '当前仅展示与该层级对应的报告正文';
        }
        renderFourPillarsAuditBlock(level);
    }
    function setLevel(level) {
        reportLevel = level || 'OUTLINE';
        if (reportLevel === 'OUTLINE') currentMode = 'doc';
        ['btnLevelOutline','btnLevelL1','btnLevelL2','btnLevelL3','btnLevelValidation','btnLevelImpact'].forEach(function(id) {
            var b = document.getElementById(id);
            if (!b) return;
            var isActive = b.getAttribute('data-level') === reportLevel;
            b.classList.toggle('active', isActive);
            if (id === 'btnLevelImpact') {
                b.classList.toggle('btn-warning', isActive);
                b.classList.toggle('btn-outline-warning', !isActive);
            } else if (id === 'btnLevelValidation') {
                b.classList.toggle('btn-info', isActive);
                b.classList.toggle('text-white', isActive);
                b.classList.toggle('btn-outline-info', !isActive);
            } else {
                b.classList.toggle('btn-primary', isActive);
                b.classList.toggle('btn-outline-secondary', !isActive);
            }
        });
        var hint = document.getElementById('currentLevelHint');
        if (hint) hint.textContent = '当前展示: ' + (levelNames[reportLevel] || reportLevel);
        
        // 切换到 OUTLINE 时，强制用“可读大纲”重绘，避免展示上一次遗留的专业审计内容
        if (reportLevel === 'OUTLINE') {
            if (contentOutlineMainMetaEl) contentOutlineMainMetaEl.textContent = '';
            if (contentOutlineMainContentEl) contentOutlineMainContentEl.innerHTML = '';
            try { renderMainOutline(); } catch (e) {}
        }

        var content = '';
        if (reportLevel === 'OUTLINE') {
            content = reports.OUTLINE || '';
        } else if (reportLevel === 'IMPACT' || reportLevel === 'VALIDATION') {
            content = ''; // IMPACT and VALIDATION are purely dynamic, no report text needed in main area
        } else {
            content = reports[reportLevel] || reports.L3 || '';
        }
        if (reportLevel !== 'IMPACT' && reportLevel !== 'VALIDATION' && content) {
            lastReport = content;
            if (reportLevel === 'OUTLINE') {
                // 大纲以结构化区（contentOutlineMainWrap）为准，不再重复渲染同一份 Markdown
                outputEl.innerHTML = '';
                outputEl.style.display = 'none';
            } else {
                outputEl.innerHTML = marked.parse(content);
                outputEl.style.display = (reportLevel === 'L3' ? (currentMode === 'doc') : true) ? 'block' : 'none';
            }
        } else {
            lastReport = '';
            outputEl.innerHTML = '';
            if (reportLevel === 'OUTLINE') {
                outputEl.style.display = 'none';
            }
        }

        // 处理总览和卡片渲染
        if (summaryData) {
            renderAuditDashboard(summaryData, defectsData);
            if (reportLevel === 'L3' && currentMode === 'card') {
                renderDefectCards(defectsData);
                cardAreaEl.style.display = 'block';
                outputEl.style.display = 'none';
            } else {
                cardAreaEl.style.display = 'none';
                if (reportLevel === 'OUTLINE' || reportLevel === 'IMPACT' || reportLevel === 'VALIDATION') {
                    outputEl.style.display = 'none';
                } else {
                    outputEl.style.display = 'block';
                }
            }
            if (modeSwitchEl) modeSwitchEl.style.display = reportLevel === 'L3' ? 'block' : 'none';
        } else {
            cardAreaEl.style.display = 'none';
            if (modeSwitchEl) modeSwitchEl.style.display = 'none';
            if (reportLevel === 'OUTLINE' || reportLevel === 'IMPACT' || reportLevel === 'VALIDATION') {
                outputEl.style.display = 'none';
            }
        }
        applyLevelVisibility(reportLevel);
    }

    // 模式切换
    document.querySelectorAll('.filter-chip').forEach(function(btn) {
        btn.addEventListener('click', function() {
            document.querySelectorAll('.filter-chip').forEach(function(b){ b.classList.remove('active'); });
            this.classList.add('active');
            var cat = this.getAttribute('data-category');
            renderDefectCards(defectsData, cat);
        });
    });
    document.getElementById('btnLevelL1').addEventListener('click', function() { setLevel('L1'); });
    document.getElementById('btnLevelL2').addEventListener('click', function() { setLevel('L2'); });
    document.getElementById('btnLevelL3').addEventListener('click', function() { setLevel('L3'); });
    document.getElementById('btnLevelOutline').addEventListener('click', function() { setLevel('OUTLINE'); });
    var btnLevelValidation = document.getElementById('btnLevelValidation');
    if (btnLevelValidation) {
        btnLevelValidation.addEventListener('click', function() { setLevel('VALIDATION'); });
    }
    var btnLevelImpact = document.getElementById('btnLevelImpact');
    if (btnLevelImpact) {
        btnLevelImpact.addEventListener('click', function() { setLevel('IMPACT'); });
    }

    var btnAnalyzeImpact = document.getElementById('btnAnalyzeImpact');
    if (btnAnalyzeImpact) {
        btnAnalyzeImpact.addEventListener('click', function() {
            var input = document.getElementById('changeImpactInput').value.trim();
            if (!input) {
                alert('请输入需求变更描述');
                return;
            }
            if (!reports.architecture_scan) {
                alert('缺少架构透视数据，请先执行“一键体检/深度审计”。');
                return;
            }
            
            var textSpan = document.getElementById('btnAnalyzeImpactText');
            var spinner = document.getElementById('btnAnalyzeImpactSpinner');
            var resultWrap = document.getElementById('changeImpactResultWrap');
            
            btnAnalyzeImpact.disabled = true;
            textSpan.textContent = '分析中...';
            spinner.classList.remove('d-none');
            
            fetch(baseUrl + '/api/analyze_impact', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ 
                    change_desc: input,
                    scan_result: reports.architecture_scan
                })
            })
            .then(res => res.json())
            .then(data => {
                if (data.code === 0 || data.status === 'success') {
                    resultWrap.style.display = 'block';
                    var mdReport = data.impact_report || (data.data && data.data.impact_report);
                    resultWrap.innerHTML = window.marked ? window.marked.parse(mdReport) : mdReport;
                } else {
                    alert('分析失败：' + (data.message || data.msg || '未知错误'));
                }
            })
            .catch(err => {
                alert('请求异常：' + err);
            })
            .finally(() => {
                btnAnalyzeImpact.disabled = false;
                textSpan.textContent = '开始分析';
                spinner.classList.add('d-none');
            });
        });
            .then(data => {
                if (data.status === 'success') {
                    resultWrap.style.display = 'block';
                    resultWrap.innerHTML = window.marked ? window.marked.parse(data.impact_report) : data.impact_report;
                } else {
                    alert('分析失败：' + data.message);
                }
            })
            .catch(err => {
                alert('请求异常：' + err);
            })
            .finally(() => {
                btnAnalyzeImpact.disabled = false;
                textSpan.textContent = '开始分析';
                spinner.classList.add('d-none');
            });
        });
    }

    // ---------- LLM 配置：获取与保存 ----------
    function applyProviderDefaults(isOpenInit) {
        try {
            var provider = (llmProviderInput.value || 'deepseek').toLowerCase();
            var base = (llmBaseUrlInput.value || '').trim();
            var model = (llmModelInput.value || '').trim();
            var hint = document.getElementById('llmApiKeyHint');
            if (provider === 'volcengine') {
                llmApiKeyInput.placeholder = 'api-key-...';
                if (hint) hint.textContent = '火山引擎 Key 通常以 api-key- 开头；保存后写入 modules/test_case/llm_config.json（全平台共用）。';
                if (isOpenInit || !base || base.indexOf('api.deepseek.com') >= 0) llmBaseUrlInput.value = 'https://ark.cn-beijing.volces.com/api/v3';
                if (isOpenInit || !model || model === 'deepseek-chat') llmModelInput.value = 'doubao-pro-32k';
            } else if (provider === 'gemini') {
                llmApiKeyInput.placeholder = 'AIza...（Gemini Key）';
                if (hint) hint.textContent = 'Gemini 使用 Google API Key；保存后写入 modules/test_case/llm_config.json（全平台共用）。';
            } else {
                llmApiKeyInput.placeholder = 'sk-...';
                if (hint) hint.textContent = 'DeepSeek Key 通常以 sk- 开头；保存后写入 modules/test_case/llm_config.json（全平台共用）。';
                if (isOpenInit || !base || base.indexOf('volces.com') >= 0 || base.indexOf('ark.cn') >= 0) llmBaseUrlInput.value = 'https://api.deepseek.com/v1';
                if (isOpenInit || !model || model.indexOf('doubao') === 0) llmModelInput.value = 'deepseek-chat';
            }
        } catch (e) { /* ignore */ }
    }

    function loadLLMConfig() {
        llmConfigStatus.textContent = '';
        fetch(baseUrl + '/api/llm_config', { method: 'GET' })
            .then(function(r) { return r.json(); })
            .then(function(res) {
                var cfg = (res && res.data) || {};
                if (!cfg || typeof cfg !== 'object') cfg = {};
                llmProviderInput.value = (cfg.llm_provider || 'deepseek').toLowerCase();
                llmBaseUrlInput.value = cfg.base_url || (llmProviderInput.value === 'volcengine' ? 'https://ark.cn-beijing.volces.com/api/v3' : 'https://api.deepseek.com/v1');
                llmModelInput.value = cfg.model || (llmProviderInput.value === 'volcengine' ? 'doubao-pro-32k' : 'deepseek-chat');
                llmApiKeyInput.value = cfg.api_key || '';
                llmFallbackEnabledInput.checked = !!cfg.fallback_enabled;
                llmFallbackBaseUrlInput.value = cfg.fallback_base_url || '';
                llmFallbackModelInput.value = cfg.fallback_model || '';
                applyProviderDefaults(true);
            })
            .catch(function(err) {
                llmConfigStatus.textContent = '加载失败：' + (err.message || String(err));
            });
    }

    function saveLLMConfig() {
        llmConfigStatus.textContent = '保存中…';
        var payload = {
            llm_provider: llmProviderInput.value || 'deepseek',
            base_url: llmBaseUrlInput.value.trim(),
            model: llmModelInput.value.trim(),
            api_key: llmApiKeyInput.value.trim(),
            fallback_enabled: !!llmFallbackEnabledInput.checked,
            fallback_base_url: llmFallbackBaseUrlInput.value.trim(),
            fallback_model: llmFallbackModelInput.value.trim()
        };
        fetch(baseUrl + '/api/llm_config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        })
        .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, body: d }; }); })
        .then(function(resp) {
            if (!resp.ok) throw new Error((resp.body && resp.body.message) || '保存失败');
            llmConfigStatus.textContent = '已保存';
        })
        .catch(function(err) {
            llmConfigStatus.textContent = '保存失败：' + (err.message || String(err));
        });
    }

    var btnLLMConfig = document.getElementById('btnLLMConfig');
    if (btnLLMConfig) {
        btnLLMConfig.addEventListener('click', function() {
            loadLLMConfig();
            var modal = new bootstrap.Modal(document.getElementById('llmConfigModal'));
            modal.show();
        });
    }
    var btnSaveLLMConfig = document.getElementById('btnSaveLLMConfig');
    if (btnSaveLLMConfig) {
        btnSaveLLMConfig.addEventListener('click', saveLLMConfig);
    }
    if (llmProviderInput) {
        llmProviderInput.addEventListener('change', function() { applyProviderDefaults(false); });
    }

    btnGenerate.addEventListener('click', function() {
        var content = (inputEl.value || '').trim();
        if (!content) {
            alert('请先输入飞书文档链接或粘贴 PRD 全文，或上传 PDF/Word。');
            return;
        }
        btnGenerate.disabled = true;
        outputEl.textContent = '';
        reports = { OUTLINE: '', L1: '', L2: '', L3: '' };
        summaryData = null;
        scanMetaData = null;
        defectsData = [];
        if (dashboardEl) dashboardEl.style.display = 'none';
        if (cardAreaEl) cardAreaEl.style.display = 'none';
        if (modeSwitchEl) modeSwitchEl.style.display = 'none';
        
        testMatrix = null;
        diagrams = null;
        extrasQuality = null;
        kg = null;
        outlineEngine = null;
        platformImpact = null;
        dependencyAnalysis = null;
        prdQuality = null;
        testPoints = null;
        validationOutline = null;
        riskPrediction = null;
        sharedSummary = null;
        readerGuide = null;
        activeLinkedTestPointId = '';
        parseMeta = null;
        featureMindmapCode = '';
        if (featureMapMainWrapEl) featureMapMainWrapEl.style.display = 'none';
        if (sharedSummaryMainWrapEl) sharedSummaryMainWrapEl.style.display = 'none';
        if (contentOutlineMainWrapEl) contentOutlineMainWrapEl.style.display = 'none';
        if (platformImpactMainWrapEl) platformImpactMainWrapEl.style.display = 'none';
        lastReport = '';
        renderExtras();
        // 清除上一轮写入的矩阵快照，避免独立矩阵页 / 本地缓存仍显示上一份 PRD（如星耀屏）
        try { localStorage.removeItem(MATRIX_SNAPSHOT_KEY); } catch (e) {}
        showStatus('PRD 三段式分析中，约 1～2 分钟…');
        var abort = new AbortController();
        // 流式读取：实时把 status 和错误显示在报告区，避免“点击后只走后台无反馈”
        function consumeStream(r) {
            if (!r.ok) return r.json().then(function(d) { throw new Error(d.message || r.statusText); });
            if (!r.body) return r.text().then(finishWithFullText);
            var decoder = new TextDecoder();
            var buf = '';
            var reader = r.body.getReader();
            function processChunk(result) {
                if (result.done) {
                    if (buf.trim()) buf.split('\n').filter(Boolean).forEach(processLine);
                    showReportButtons();
                    renderExtras();
                    return;
                }
                buf += decoder.decode(result.value, { stream: true });
                var parts = buf.split('\n');
                buf = parts.pop() || '';
                parts.forEach(processLine);
                return reader.read().then(processChunk);
            }
            return reader.read().then(processChunk);
        }
        function processLine(line) {
            line = (line || '').trim();
            if (!line) return;
            try {
                var obj = JSON.parse(line);
                if (obj.type === 'status') {
                    var t = obj.text || '';
                    if (t) { outputEl.textContent += t; outputEl.scrollTop = outputEl.scrollHeight; }
                } else if (obj.type === 'bundle') {
                    reports.L1 = obj.L1 || '';
                    reports.L2 = obj.L2 || '';
                    reports.L3 = obj.L3 || '';
                    reports.architecture_scan = obj.architecture_scan || null;
                    reports.validation_outline = obj.validation_outline || null;
                    summaryData = obj.summary || null;
                    defectsData = obj.defects || [];
                    scanMetaData = (obj.scan_meta && typeof obj.scan_meta === 'object') ? obj.scan_meta : null;
                    
                    if (obj.outline_llm && obj.outline_llm.ok) {
                        llmFourPillarsPayload = { llm: obj.outline_llm, stage1_output: obj.parse_meta };
                    }

                    testMatrix = obj.test_matrix || null;
                    diagrams = obj.diagrams || null;
                    extrasQuality = obj.extras_quality || null;
                    kg = obj.kg || null;
                    outlineEngine = obj.outline_engine || null;
                    platformImpact = obj.platform_impact || null;
                    dependencyAnalysis = obj.dependency_analysis || null;
                    prdQuality = obj.prd_quality || null;
                    testPoints = obj.test_points || null;
                    validationOutline = obj.validation_outline || null;
                    riskPrediction = obj.risk_prediction || null;
                    understandingCards = obj.understanding_cards || null;
                    releaseGate = obj.release_gate || null;
                    sharedSummary = obj.shared_summary || null;
                    readerGuide = obj.reader_guide || null;
                    parseMeta = obj.parse_meta || null;
                    
                    // 特别注意：变更影响分析依赖 architecture_scan，我们需要确保它存在
                    if (obj.architecture_scan) {
                        reports.architecture_scan = obj.architecture_scan;
                        console.log("已获取并保存架构透视数据，可用于变更影响分析", reports.architecture_scan);
                    }
                    
                    reports.OUTLINE = buildOutlineReportMarkdown();
                    var level = reportLevel || 'L3';
                    if (!reports[level]) level = 'L3';
                    reportLevel = level;
                    lastReport = reports[level] || outputEl.textContent;
                    outputEl.textContent = lastReport;
                    setLevel(level);
                    showReportButtons();
                    
                    // 确保验证大纲在完成后被渲染
                    if (validationOutline) {
                        renderValidationOutline();
                    }
                    
                    renderExtras();
                    if (buildFeatureNodes().length > 0) {
                        focusFeatureMapTab();
                    }
                } else if (obj.type === 'error') {
                    outputEl.textContent += '\n[错误] ' + (obj.text || '');
                    outputEl.scrollTop = outputEl.scrollHeight;
                }
            } catch (e) {
                if (line) { outputEl.textContent += line + '\n'; outputEl.scrollTop = outputEl.scrollHeight; }
            }
        }
        function finishWithFullText(text) {
            var lines = text.split('\n').filter(Boolean);
            lines.forEach(processLine);
            if (!reports.L1 && !reports.L2 && !reports.L3) {
                lastReport = outputEl.textContent;
                setLevel(reportLevel || 'L3');
            }
            showReportButtons();
            
            // 确保验证大纲在完成后被渲染
            if (validationOutline) {
                renderValidationOutline();
            }
            
            // 确保变更影响分析相关的 UI 正确显示
            if (reports.architecture_scan) {
                console.log("已获取到架构透视数据，变更影响分析可用");
            }
            
            renderExtras();
        }

        fetch(baseUrl + '/api/generate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                type: 'prd_review',
                content: content,
                use_llm: !useLLMCheckbox || !!useLLMCheckbox.checked
            }),
            signal: abort.signal
        })
        .then(consumeStream)
        .catch(function(err) {
            if (err.name === 'AbortError') return;
            outputEl.textContent = (outputEl.textContent || '') + '\n[请求失败] ' + (err.message || String(err));
        })
        .finally(function() {
            btnGenerate.disabled = false;
            showStatus('');
        });
    });

    btnCopy.addEventListener('click', function() {
        if (!lastReport) return;
        navigator.clipboard && navigator.clipboard.writeText(lastReport).then(function() { alert('已复制到剪贴板'); }).catch(function() { alert('复制失败'); });
    });

    function getCurrentContent() {
        var c = reports[reportLevel] || reports.L3 || '';
        return c || lastReport;
    }
    function downloadMd(level) {
        var content = level ? (reports[level] || '') : getCurrentContent();
        if (!content) { alert('该层级暂无内容'); return; }
        var a = document.createElement('a');
        a.href = 'data:text/markdown;charset=utf-8,' + encodeURIComponent(content);
        a.download = 'PRD审计报告_' + (level || reportLevel) + '_' + new Date().toISOString().slice(0,10) + '.md';
        a.click();
    }
    function downloadWord(level) {
        var content = level ? (reports[level] || '') : getCurrentContent();
        if (!content) { alert('该层级暂无内容'); return; }
        fetch(baseUrl + '/api/export_report_docx', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ content: content })
        })
        .then(function(r) {
            if (!r.ok) return r.json().then(function(d) { throw new Error(d.message || r.statusText); });
            return r.blob();
        })
        .then(function(blob) {
            var a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = 'PRD审计报告_' + (level || reportLevel) + '_' + new Date().toISOString().slice(0,10) + '.docx';
            a.click();
            URL.revokeObjectURL(a.href);
        })
        .catch(function(err) { alert('导出 Word 失败: ' + err.message); });
    }
    btnDownloadMd.addEventListener('click', function() { downloadMd(); });
    btnDownloadWord.addEventListener('click', function() { downloadWord(); });
    
    var btnCopyLevelContent = document.getElementById('btnCopyLevelContent');
    if (btnCopyLevelContent) {
        btnCopyLevelContent.addEventListener('click', function() {
            var contentToCopy = '';
            if (reportLevel === 'OUTLINE') {
                // 如果在认知大纲，先收集四段式摘要和结构文本
                var sharedWrap = document.getElementById('sharedSummaryMainContent');
                var text = [];
                if (sharedWrap && sharedWrap.innerText.trim()) {
                    text.push("【全员共识摘要】");
                    text.push(sharedWrap.innerText.trim());
                    text.push("");
                }
                if (outlineData && typeof outlineData === 'object') {
                    text.push("【认知大纲】");
                    text.push(JSON.stringify(outlineData, null, 2));
                }
                contentToCopy = text.join('\n');
            } else {
                contentToCopy = getCurrentContent();
            }
            if (!contentToCopy) {
                showNonBlockingToast('当前层级暂无内容可复制', 'warning');
                return;
            }
            navigator.clipboard && navigator.clipboard.writeText(contentToCopy).then(function() {
                showNonBlockingToast('已复制当前层级内容给PM', 'success');
            }).catch(function() {
                alert('复制失败，请重试');
            });
        });
    }

    document.querySelectorAll('[data-dl][data-fmt]').forEach(function(el) {
        el.addEventListener('click', function(e) {
            e.preventDefault();
            var level = el.getAttribute('data-dl');
            var fmt = el.getAttribute('data-fmt');
            if (fmt === 'md') downloadMd(level); else downloadWord(level);
        });
    });

    if (btnMatrixFullView) {
        btnMatrixFullView.addEventListener('click', function() {
            if (!testMatrix || typeof testMatrix !== 'object') {
                alert('暂无测试矩阵数据，请先执行 PRD 分析。');
                return;
            }
            matrixFullViewModal.show();
        });
    }

    if (btnMatrixStandalone) {
        btnMatrixStandalone.addEventListener('click', function() {
            if (!testMatrix || typeof testMatrix !== 'object') {
                alert('暂无测试矩阵数据，请先执行 PRD 分析。');
                return;
            }
            saveMatrixSnapshot();
            window.location.href = '/prd_audit/matrix_view';
        });
    }

    if (btnMatrixTopEntry) {
        btnMatrixTopEntry.addEventListener('click', function() {
            if (testMatrix && typeof testMatrix === 'object') {
                saveMatrixSnapshot();
            }
            window.location.href = '/prd_audit/matrix_view';
        });
    }

    if (btnExportTestPoints) {
        btnExportTestPoints.addEventListener('click', function() {
            if (!testPoints || typeof testPoints !== 'object') {
                alert('暂无测试点数据，请先执行 PRD 分析。');
                return;
            }
            var a = document.createElement('a');
            a.href = 'data:application/json;charset=utf-8,' + encodeURIComponent(JSON.stringify(testPoints, null, 2));
            a.download = 'test_points_' + new Date().toISOString().slice(0,10) + '.json';
            a.click();
        });
    }

    if (btnDownloadFeatureMap) {
        btnDownloadFeatureMap.addEventListener('click', function() {
            if (!featureMindmapCode) {
                renderFeatureMindmap();
            }
            if (!featureMindmapCode) {
                alert('暂无可导出的功能导图，请先执行 PRD 分析。');
                return;
            }
            var a = document.createElement('a');
            a.href = 'data:text/plain;charset=utf-8,' + encodeURIComponent(featureMindmapCode);
            a.download = 'PRD功能导图_' + new Date().toISOString().slice(0,10) + '.mmd';
            a.click();
        });
    }

    if (btnExportXmind) {
        btnExportXmind.addEventListener('click', function() {
            var nodes = buildFeatureNodes();
            if (!nodes.length) {
                alert('暂无可导出的功能导图，请先执行 PRD 分析。');
                return;
            }
            btnExportXmind.disabled = true;
            fetch(baseUrl + '/api/export_feature_xmind', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ nodes: nodes })
            })
            .then(function(r) {
                if (!r.ok) {
                    return r.json().then(function(d){ throw new Error((d && d.message) || r.statusText); });
                }
                return r.blob();
            })
            .then(function(blob) {
                var a = document.createElement('a');
                a.href = URL.createObjectURL(blob);
                a.download = 'PRD功能导图_' + new Date().toISOString().slice(0,10) + '.xmind';
                a.click();
                URL.revokeObjectURL(a.href);
            })
            .catch(function(err) {
                alert('导出 XMind 失败: ' + (err.message || String(err)));
            })
            .finally(function() {
                btnExportXmind.disabled = false;
            });
        });
    }

    if (btnRefreshKnowledge) {
        btnRefreshKnowledge.addEventListener('click', function() {
            loadKnowledgeCards();
        });
    }

    if (btnKnowledgeAdd) {
        btnKnowledgeAdd.addEventListener('click', function() {
            openKnowledgeCardEditor(getKnowledgeTemplate(), -1);
        });
    }

    if (btnKnowledgeCardSave) {
        btnKnowledgeCardSave.addEventListener('click', function() {
            try {
                var parsed = JSON.parse((knowledgeCardJson && knowledgeCardJson.value) || '{}');
                if (!parsed || typeof parsed !== 'object') throw new Error('JSON 必须是对象');
                if (!String(parsed.capability_id || '').trim()) throw new Error('capability_id 不能为空');
                if (!String(parsed.name || '').trim()) throw new Error('name 不能为空');
                if (editingKnowledgeIndex >= 0 && editingKnowledgeIndex < knowledgeCards.length) {
                    knowledgeCards[editingKnowledgeIndex] = parsed;
                } else {
                    knowledgeCards.unshift(parsed);
                }
                renderKnowledgeCards();
                renderExtras();
                if (knowledgeCardStatus) knowledgeCardStatus.textContent = '已写入本地列表，记得点击“保存”';
                setTimeout(function() {
                    if (knowledgeCardModal) knowledgeCardModal.hide();
                }, 300);
            } catch (e) {
                if (knowledgeCardStatus) knowledgeCardStatus.textContent = '保存失败：' + (e.message || String(e));
            }
        });
    }

    if (btnKnowledgeSaveAll) {
        btnKnowledgeSaveAll.addEventListener('click', function() {
            btnKnowledgeSaveAll.disabled = true;
            saveKnowledgeCards()
                .then(function() { alert('能力库已保存'); })
                .catch(function(err) { alert('保存失败：' + (err.message || String(err))); })
                .finally(function() { btnKnowledgeSaveAll.disabled = false; });
        });
    }

    if (btnKnowledgeExport) {
        btnKnowledgeExport.addEventListener('click', function() {
            window.location.href = '/prd_audit/api/knowledge_cards/export?format=csv';
        });
    }

    if (btnKnowledgeImport && knowledgeImportInput) {
        btnKnowledgeImport.addEventListener('click', function() {
            knowledgeImportInput.click();
        });
        knowledgeImportInput.addEventListener('change', function() {
            var file = knowledgeImportInput.files && knowledgeImportInput.files[0];
            if (!file) return;
            
            var isJson = file.name.toLowerCase().endsWith('.json');
            
            if (isJson) {
                var reader = new FileReader();
                reader.onload = function() {
                    try {
                        var parsed = JSON.parse(String(reader.result || '{}'));
                        var items = Array.isArray(parsed) ? parsed : (Array.isArray(parsed.items) ? parsed.items : null);
                        if (!items) throw new Error('JSON 格式需为数组或 {items:[...]}');
                        knowledgeCards = items;
                        renderKnowledgeCards();
                        renderExtras();
                        alert('导入成功，请点击“保存”写入服务端');
                    } catch (e) {
                        alert('导入失败：' + (e.message || String(e)));
                    } finally {
                        knowledgeImportInput.value = '';
                    }
                };
                reader.readAsText(file, 'utf-8');
            } else {
                var form = new FormData();
                form.append('file', file);
                fetch('/prd_audit/api/knowledge_cards/import', {
                    method: 'POST',
                    body: form
                })
                .then(function(r){ return r.json(); })
                .then(function(res){
                    if (!res || !res.success) throw new Error((res && res.message) || '导入失败');
                    loadKnowledgeCards();
                    alert('导入成功：' + ((res.data && res.data.count) || 0) + ' 条');
                })
                .catch(function(err){ alert('CSV 导入失败：' + (err.message || String(err))); })
                .finally(function(){ knowledgeImportInput.value = ''; });
            }
        });
    }

    loadKnowledgeCards();

    document.addEventListener('click', function(e) {
        if (!e.target || !e.target.closest) return;
        var copyFixBtn = e.target.closest('.btn-copy-must-fix');
        if (copyFixBtn) {
            if (!releaseGate || typeof releaseGate !== 'object') return;
            var decision = String(releaseGate.decision || 'REVIEW').toUpperCase();
            var reasons = Array.isArray(releaseGate.reasons) ? releaseGate.reasons : [];
            var fixes = Array.isArray(releaseGate.must_fix) ? releaseGate.must_fix : [];
            var lines = [];
            lines.push('发布决策：' + decision);
            if (reasons.length) lines.push('关键原因：' + reasons[0]);
            fixes.slice(0, 2).forEach(function(x, idx) { lines.push('整改' + (idx + 1) + '：' + x); });
            var text = lines.join('\n');
            var onOk = function(){ showNonBlockingToast('整改清单已复制', 'success'); };
            var onFail = function(){ showNonBlockingToast('复制失败，请手动复制', 'danger'); };
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(text).then(onOk).catch(function() {
                    try {
                        var ta = document.createElement('textarea');
                        ta.value = text;
                        document.body.appendChild(ta);
                        ta.select();
                        document.execCommand('copy');
                        document.body.removeChild(ta);
                        onOk();
                    } catch (_) { onFail(); }
                });
            } else {
                try {
                    var ta2 = document.createElement('textarea');
                    ta2.value = text;
                    document.body.appendChild(ta2);
                    ta2.select();
                    document.execCommand('copy');
                    document.body.removeChild(ta2);
                    onOk();
                } catch (_) { onFail(); }
            }
            return;
        }
        var openTabBtn = e.target.closest('.btn-open-left-tab');
        if (openTabBtn) {
            var targetTab = String(openTabBtn.getAttribute('data-target') || '').trim();
            if (targetTab) showLeftTab(targetTab);
            return;
        }
        var clearBtn = e.target.closest('.btn-clear-linkage');
        if (clearBtn) {
            activeLinkedTestPointId = '';
            applyRiskTestPointHighlight();
            return;
        }
        var riskLink = e.target.closest('.rp-testpoint-link');
        if (riskLink) {
            var tpIdFromRisk = String(riskLink.getAttribute('data-tpid') || '').trim();
            if (!tpIdFromRisk) return;
            activeLinkedTestPointId = tpIdFromRisk;
            applyRiskTestPointHighlight();
            showLeftTab('#leftPaneTestPoints');
            var row = document.querySelector('.tp-row[data-tp-id="' + cssEscapeText(tpIdFromRisk) + '"]');
            if (row && row.scrollIntoView) row.scrollIntoView({ behavior: 'smooth', block: 'center' });
            return;
        }
        var tpLink = e.target.closest('.tp-id-link');
        if (tpLink) {
            var tpId = String(tpLink.getAttribute('data-tpid') || '').trim();
            if (!tpId) return;
            activeLinkedTestPointId = tpId;
            applyRiskTestPointHighlight();
            showLeftTab('#leftPaneRisk');
            var riskRow = document.querySelector('.risk-row.table-warning');
            if (riskRow && riskRow.scrollIntoView) riskRow.scrollIntoView({ behavior: 'smooth', block: 'center' });
            return;
        }
        var riskRowClick = e.target.closest('.risk-row');
        if (riskRowClick) {
            var related = String(riskRowClick.getAttribute('data-related') || '').trim();
            var first = related ? String(related.split(',')[0] || '').trim() : '';
            if (!first) return;
            activeLinkedTestPointId = first;
            applyRiskTestPointHighlight();
            showLeftTab('#leftPaneTestPoints');
            var row2 = document.querySelector('.tp-row[data-tp-id="' + cssEscapeText(first) + '"]');
            if (row2 && row2.scrollIntoView) row2.scrollIntoView({ behavior: 'smooth', block: 'center' });
            return;
        }
        var tpRowClick = e.target.closest('.tp-row');
        if (tpRowClick) {
            var clickedTp = String(tpRowClick.getAttribute('data-tp-id') || '').trim();
            if (!clickedTp) return;
            activeLinkedTestPointId = clickedTp;
            applyRiskTestPointHighlight();
            showLeftTab('#leftPaneRisk');
            var riskRow2 = document.querySelector('.risk-row.table-warning');
            if (riskRow2 && riskRow2.scrollIntoView) riskRow2.scrollIntoView({ behavior: 'smooth', block: 'center' });
            return;
        }
    });

    document.addEventListener('click', function(e) {
        if (!e.target || !e.target.closest) return;
        var editBtn = e.target.closest('.btn-knowledge-edit');
        if (editBtn) {
            var editIdx = Number(editBtn.getAttribute('data-idx'));
            var card = knowledgeCards[editIdx];
            if (card) openKnowledgeCardEditor(card, editIdx);
            return;
        }
        var delBtn = e.target.closest('.btn-knowledge-delete');
        if (delBtn) {
            var delIdx = Number(delBtn.getAttribute('data-idx'));
            if (Number.isFinite(delIdx) && delIdx >= 0 && delIdx < knowledgeCards.length) {
                knowledgeCards.splice(delIdx, 1);
                renderKnowledgeCards();
                renderExtras();
            }
            return;
        }
    });

    // Matrix Modal Listener
    document.addEventListener('click', function(e) {
        if (!e.target || !e.target.closest) return;
        var filterBtn = e.target.closest('.btn-matrix-filter');
        if (filterBtn) {
            var f = String(filterBtn.getAttribute('data-filter') || '').trim();
            if (f === 'missing') matrixFilters.missing = !matrixFilters.missing;
            if (f === 'p0') matrixFilters.p0 = !matrixFilters.p0;
            renderMatrixFullView(buildMatrixSections({ onlyMissing: matrixFilters.missing, onlyP0: matrixFilters.p0 }));
            return;
        }
        var jumpBtn = e.target.closest('.btn-matrix-jump');
        if (jumpBtn) {
            var targetId = String(jumpBtn.getAttribute('data-target') || '').trim();
            if (!targetId) return;
            var targetEl = document.getElementById(targetId);
            if (targetEl && targetEl.scrollIntoView) {
                targetEl.scrollIntoView({ behavior: 'smooth', block: 'start' });
            }
            return;
        }
        var btn = e.target.closest('.btn-matrix-detail');
        if (!btn) return;
        
        var caseId = btn.getAttribute('data-case') || '-';
        var status = btn.getAttribute('data-status') || '';
        var risk = btn.getAttribute('data-risk') || '';
        var exp = btn.getAttribute('data-exp') || '无';
        var evi = btn.getAttribute('data-evi') || '无';
        var sug = btn.getAttribute('data-sug') || '';

        var mdCaseId = document.getElementById('mdCaseId');
        if (mdCaseId) mdCaseId.textContent = caseId;
        
        var mdExpected = document.getElementById('mdExpected');
        if (mdExpected) mdExpected.textContent = exp;
        
        var mdEvidence = document.getElementById('mdEvidence');
        if (mdEvidence) mdEvidence.textContent = evi;
        
        var mdSuggestion = document.getElementById('mdSuggestion');
        if (mdSuggestion) mdSuggestion.textContent = sug || '无建议';
        
        var sugWrap = document.getElementById('mdSuggestionWrap');
        if (sugWrap) sugWrap.style.display = sug ? 'block' : 'none';

        var statusHtml = '<span class="badge bg-secondary">未知</span>';
        if (status === '缺失') statusHtml = '<span class="badge bg-danger"><i class="fas fa-times-circle me-1"></i>缺失</span>';
        else if (status === '覆盖') statusHtml = '<span class="badge bg-success"><i class="fas fa-check-circle me-1"></i>覆盖</span>';
        else if (status === '待确认') statusHtml = '<span class="badge bg-warning text-dark"><i class="fas fa-question-circle me-1"></i>待确认</span>';
        
        var badgeContainer = document.getElementById('mdStatusBadges');
        if (badgeContainer) {
            badgeContainer.innerHTML = statusHtml + (risk ? (' <span class="badge ' + (risk==='P0'?'bg-danger':(risk==='P1'?'bg-warning text-dark':'bg-primary')) + ' ms-1">' + risk + '</span>') : '');
        }
        
        var modalEl = document.getElementById('matrixDetailModal');
        if (modalEl) {
            var modal = new bootstrap.Modal(modalEl);
            modal.show();
        }
    });

})();

// ========== AI智能助手 ==========
(function() {
    // 创建智能助手UI
    var widget = document.createElement('div');
    widget.className = 'ai-assistant-widget collapsed';
    widget.id = 'aiAssistantWidget';
    widget.innerHTML = `
        <div class="ai-assistant-header" id="aiAssistantHeader">
            <div class="ai-assistant-title">
                <i class="fas fa-robot"></i>
                <span id="aiAssistantTitleText">AI助手</span>
            </div>
            <div>
                <button class="btn btn-sm btn-link text-white p-0" id="aiAssistantToggle" style="text-decoration:none;">
                    <i class="fas fa-expand"></i>
                </button>
            </div>
        </div>
        <div class="ai-assistant-body" id="aiAssistantBody">
            <div id="aiAssistantMessages"></div>
        </div>
        <div class="ai-assistant-input-area">
            <div class="input-group input-group-sm">
                <input type="text" class="form-control" id="aiAssistantInput" placeholder="输入指令，如：审计这个PRD">
                <button class="btn btn-primary" id="aiAssistantSend"><i class="fas fa-paper-plane"></i></button>
            </div>
            <div class="ai-quick-actions mt-2">
                <button class="ai-quick-btn" data-action="audit">🔍 审计PRD</button>
                <button class="ai-quick-btn" data-action="impact">📊 变更影响</button>
                <button class="ai-quick-btn" data-action="testcase">📝 生成用例</button>
                <button class="ai-quick-btn" data-action="jira">🐛 查Jira</button>
            </div>
        </div>
    `;
    document.body.appendChild(widget);

    // 状态管理
    var isExpanded = false;
    var messages = [];
    var isProcessing = false;

    // DOM元素
    var header = document.getElementById('aiAssistantHeader');
    var toggleBtn = document.getElementById('aiAssistantToggle');
    var body = document.getElementById('aiAssistantBody');
    var messagesEl = document.getElementById('aiAssistantMessages');
    var inputEl = document.getElementById('aiAssistantInput');
    var sendBtn = document.getElementById('aiAssistantSend');

    // 展开/收起
    function toggle() {
        isExpanded = !isExpanded;
        widget.classList.toggle('collapsed', !isExpanded);
        toggleBtn.innerHTML = isExpanded ? '<i class="fas fa-compress"></i>' : '<i class="fas fa-expand"></i>';
        if (isExpanded && messages.length === 0) {
            addMessage('assistant', '你好！我是PRD审计助手。\n\n你可以对我说：\n• "审计这个PRD"\n• "分析变更影响"\n• "生成测试用例"\n• "查一下Jira Bug"\n\n或者直接点击下方的快捷按钮。');
        }
    }

    header.addEventListener('click', function(e) {
        if (e.target.closest('#aiAssistantToggle')) return;
        toggle();
    });
    toggleBtn.addEventListener('click', toggle);

    // 添加消息
    function addMessage(role, content, isRawHtml) {
        var msgDiv = document.createElement('div');
        msgDiv.className = 'ai-message ' + role;
        var avatar = role === 'user' ? '👤' : '🤖';
        
        var contentHtml = '';
        if (isRawHtml === true) {
            contentHtml = content; // 后端直接返回解析好的 html 或带有标签的错误信息
        } else {
            contentHtml = escapeHtml(content).replace(/\n/g, '<br>');
            // 兼容旧代码里把 extra 拼到最后面的逻辑（如果有的话，现在基本不用了）
            if (typeof isRawHtml === 'string') {
                contentHtml += isRawHtml;
            }
        }
        
        msgDiv.innerHTML = `
            <div class="ai-message-avatar">${avatar}</div>
            <div class="ai-message-content">${contentHtml}</div>
        `;
        messagesEl.appendChild(msgDiv);
        messagesEl.scrollTop = messagesEl.scrollHeight;
        messages.push({role: role, content: content});
    }

    // 显示输入中
    function showTyping() {
        var typingDiv = document.createElement('div');
        typingDiv.className = 'ai-message assistant';
        typingDiv.id = 'aiTypingIndicator';
        typingDiv.innerHTML = `
            <div class="ai-message-avatar">🤖</div>
            <div class="ai-message-content">
                <div class="ai-typing">
                    <div class="ai-typing-dot"></div>
                    <div class="ai-typing-dot"></div>
                    <div class="ai-typing-dot"></div>
                </div>
            </div>
        `;
        messagesEl.appendChild(typingDiv);
        messagesEl.scrollTop = messagesEl.scrollHeight;
    }

    function hideTyping() {
        var typing = document.getElementById('aiTypingIndicator');
        if (typing) typing.remove();
    }

    // 意图解析和执行
    function parseIntent(text) {
        text = text.toLowerCase();
        
        // 审计PRD
        if (text.match(/审计|分析|检查|review|audit/)) {
            return {action: 'audit', confidence: 0.9};
        }
        // 变更影响
        if (text.match(/变更|影响|impact|change/)) {
            return {action: 'impact', confidence: 0.85};
        }
        // 生成测试用例
        if (text.match(/测试|用例|test|case/)) {
            return {action: 'testcase', confidence: 0.8};
        }
        // 查Jira
        if (text.match(/jira|bug|缺陷|问题/)) {
            return {action: 'jira', confidence: 0.75};
        }
        
        return {action: 'unknown', confidence: 0};
    }

    function executeAction(action) {
        switch(action) {
            case 'audit':
                return doAudit();
            case 'impact':
                return doImpactAnalysis();
            case 'testcase':
                return doGenerateTestCase();
            case 'jira':
                return doJiraQuery();
            default:
                return Promise.resolve('抱歉，我不太理解你的指令。你可以说"审计PRD"、"分析变更影响"等。');
        }
    }

    // 执行审计
    function doAudit() {
        var prdText = document.getElementById('prdInput')?.value?.trim();
        if (!prdText) {
            return Promise.resolve('⚠️ 请先输入或上传PRD内容，然后我再帮你审计。');
        }
        
        // 触发原有的审计按钮
        var btnBugAudit = document.getElementById('btnBugAudit');
        if (btnBugAudit) {
            btnBugAudit.click();
            return Promise.resolve('正在执行PRD审计，请稍候...\n\n完成后你可以在左侧查看审计结果。');
        }
        return Promise.resolve('审计功能暂时不可用，请稍后重试。');
    }

    // 变更影响分析
    function doImpactAnalysis() {
        var scanData = window.reports?.architecture_scan;
        if (!scanData) {
            return Promise.resolve('⚠️ 请先执行PRD审计，获取架构透视数据后，才能进行变更影响分析。');
        }
        
        // 切换到变更影响标签
        var btnLevelImpact = document.getElementById('btnLevelImpact');
        if (btnLevelImpact) {
            btnLevelImpact.click();
            return Promise.resolve('已切换到"变更影响分析"标签。\n\n请在输入框中输入变更描述，然后点击"开始分析"。');
        }
        return Promise.resolve('变更影响分析功能暂时不可用。');
    }

    // 生成测试用例
    function doGenerateTestCase() {
        var testPoints = window.testPoints;
        if (!testPoints) {
            return Promise.resolve('⚠️ 请先执行PRD审计，获取测试点数据后，才能生成测试用例。');
        }
        
        // 触发测试点导出
        var btnExportTestPoints = document.getElementById('btnExportTestPoints');
        if (btnExportTestPoints) {
            btnExportTestPoints.click();
            return Promise.resolve('已导出测试点数据，你可以在此基础上生成测试用例。');
        }
        return Promise.resolve('测试用例导出功能暂时不可用。');
    }

    // 查Jira
    function doJiraQuery() {
        // 跳转到Bug模式库页面
        var tab = document.querySelector('[data-bs-target="#tab-bug"]');
        if (tab) {
            tab.click();
            return Promise.resolve('已切换到Bug模式库。\n\n你可以在这里配置Jira导入。');
        }
        return Promise.resolve('Jira查询功能暂时不可用。');
    }

    // 发送消息
    function sendMessage() {
        if (isProcessing) return;
        
        var text = inputEl.value.trim();
        if (!text) return;
        
        addMessage('user', text);
        inputEl.value = '';
        isProcessing = true;
        showTyping();
        
        // 收集当前上下文
        var prdContent = document.getElementById('inputContent') ? document.getElementById('inputContent').value : '';
        var contextData = {
            prd_content: prdContent,
            architecture_scan: reports.architecture_scan || null,
            audit_report: reports.L3 ? true : false
        };
        
        // 调用真实的后端接口
        apiJson(baseUrl + '/api/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ message: text, context: contextData })
        }).then(function(res) {
            hideTyping();
            var replyHtml = res.reply ? (window.marked ? window.marked.parse(res.reply) : escapeHtml(res.reply)) : '请求失败';
            addMessage('assistant', replyHtml, true); // true 表示不转义直接当 html 插入
            isProcessing = false;
        }).catch(function(err) {
            hideTyping();
            addMessage('assistant', '<span class="text-danger">请求出错: ' + escapeHtml(err.message) + '</span>', true);
            isProcessing = false;
        });
    }

    sendBtn.addEventListener('click', sendMessage);
    inputEl.addEventListener('keypress', function(e) {
        if (e.key === 'Enter') sendMessage();
    });

    // 快捷按钮
    document.querySelectorAll('.ai-quick-btn').forEach(function(btn) {
        btn.addEventListener('click', function() {
            var action = this.getAttribute('data-action');
            var textMap = {
                'audit': '审计这个PRD',
                'impact': '分析变更影响',
                'testcase': '生成测试用例',
                'jira': '查Jira Bug'
            };
            inputEl.value = textMap[action] || '';
            sendMessage();
        });
    });

    // 工具函数
    function escapeHtml(s) {
        return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    console.log('AI智能助手已加载');
})();
