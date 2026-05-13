(function () {
    const PROMPT_GENERATOR_TEXT = {
        copiedSuffix: ' copied.',
        text: 'Text',
        copyFailed: 'Copy failed.',
        instructionsBlock: 'Instructions',
        workflowMarkdownBlock: 'Workflow markdown',
        doctypeBlock: 'Doctype',
        generatedPrompt: 'Generated Prompt',
        ready: 'ready',
        lines: 'lines',
        chars: 'chars',
        generateRequested: 'Generate requested workflow save + reload.',
        regeneratedLocally: 'Prompt regenerated locally.',
        nothingToCopy: 'nothing-to-copy',
        generatedPromptLabel: 'Generated prompt',
        colorHexLabel: 'Color hex'
    };

    function pgText(key) {
        const data = window.PromptRuntimeData || {};
        const fromRuntime = data && data.uiText && Object.prototype.hasOwnProperty.call(data.uiText, key) ? data.uiText[key] : null;
        return String(fromRuntime || PROMPT_GENERATOR_TEXT[key] || key);
    }

    function hasJQuery() {
        return !!(window.jQuery && window.jQuery.fn);
    }

    function bridgePrint(message) {
        const value = String(message);
        console.log(value);
        if (window.qtBridge && typeof window.qtBridge.print === 'function') {
            try { window.qtBridge.print(value); } catch (error) { console.warn('[generator][bridge-print-failed] ' + String(error && error.stack ? error.stack : error)); }
        }
    }

    function withErrorBoundary(label, fn) {
        try {
            return fn();
        } catch (error) {
            const detail = String(error && error.stack ? error.stack : error);
            console.error('[generator][error] ' + label + ': ' + detail);
            bridgePrint('[generator][error] ' + label + ': ' + detail);
            return null;
        }
    }

    function getRuntimeData() {
        return window.PromptRuntimeData || {};
    }

    function getValue(id) {
        if (hasJQuery()) {
            const node = window.jQuery('#' + id);
            return node.length ? String(node.val() || '') : '';
        }
        const node = document.getElementById(id);
        return node ? String(node.value || '') : '';
    }

    function setValue(id, value) {
        if (hasJQuery()) {
            window.jQuery('#' + id).val(value);
            return;
        }
        const node = document.getElementById(id);
        if (node) {
            node.value = value;
        }
    }

    function setText(id, value) {
        if (hasJQuery()) {
            window.jQuery('#' + id).text(value);
            return;
        }
        const node = document.getElementById(id);
        if (node) {
            node.textContent = value;
        }
    }

    function trimValue(value) {
        return String(value || '').trim();
    }

    function extractChoiceItems(text, markerRegex) {
        const source = String(text || '');
        const regex = new RegExp(markerRegex.source, markerRegex.flags && markerRegex.flags.indexOf('g') >= 0 ? markerRegex.flags : (markerRegex.flags || '') + 'g');
        const matches = [];
        let match;
        while ((match = regex.exec(source)) !== null) {
            matches.push({ index: match.index, marker: match[0], checked: String(match[1] || '') === 'O' || String(match[1] || '').toLowerCase() === 'x' });
            if (match[0] === '') { regex.lastIndex += 1; }
        }
        const items = [];
        for (let i = 0; i < matches.length; i += 1) {
            const start = matches[i].index + matches[i].marker.length;
            const end = i + 1 < matches.length ? matches[i + 1].index : source.length;
            const label = trimValue(source.slice(start, end).replace(/^[-*+]\s+/, ''));
            if (label) { items.push({ label: label, checked: !!matches[i].checked }); }
        }
        return items;
    }

    function setGeneratorStatus(value) {
        const node = document.getElementById('generatorStatus');
        if (!node) { return; }
        node.setAttribute('data-status', String(value || ''));
        node.textContent = '';
    }

    function normalizeHexColor(value) {
        let raw = String(value || '').trim();
        if (!raw) { return ''; }
        if (raw.charAt(0) !== '#') { raw = '#' + raw; }
        if (/^#[0-9a-fA-F]{3}$/.test(raw)) {
            raw = '#' + raw.charAt(1) + raw.charAt(1) + raw.charAt(2) + raw.charAt(2) + raw.charAt(3) + raw.charAt(3);
        }
        if (!/^#[0-9a-fA-F]{6}$/.test(raw)) { return ''; }
        return raw.toUpperCase();
    }

    function applyGeneratedColor(value) {
        const normalized = normalizeHexColor(value) || '#8ECBFF';
        const opened = document.getElementById('openedPrompt');
        const picker = document.getElementById('quickColorPicker');
        const hex = document.getElementById('quickColorHex');
        document.documentElement.style.setProperty('--prompt-generated-color', normalized);
        if (opened) { opened.style.color = normalized; }
        if (picker) { picker.value = normalized; }
        if (hex) { hex.value = normalized; }
        return normalized;
    }

    function copyTextWithFallback(text, label) {
        const value = String(text || '');
        if (window.qtBridge && typeof window.qtBridge.copyText === 'function') {
            try {
                window.qtBridge.copyText(value);
                setGeneratorStatus((label || pgText('text')) + pgText('copiedSuffix'));
                return Promise.resolve(true);
            } catch (error) {
                console.warn('[generator][clipboard][qt-failed] ' + String(error && error.stack ? error.stack : error));
            }
        }
        if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
            return navigator.clipboard.writeText(value).then(function () {
    const PROMPT_GENERATOR_TEXT = {
        copiedSuffix: ' copied.',
        text: 'Text',
        copyFailed: 'Copy failed.',
        instructionsBlock: 'Instructions',
        workflowMarkdownBlock: 'Workflow markdown',
        doctypeBlock: 'Doctype',
        generatedPrompt: 'Generated Prompt',
        ready: 'ready',
        lines: 'lines',
        chars: 'chars',
        generateRequested: 'Generate requested workflow save + reload.',
        regeneratedLocally: 'Prompt regenerated locally.',
        nothingToCopy: 'nothing-to-copy',
        generatedPromptLabel: 'Generated prompt',
        colorHexLabel: 'Color hex'
    };

    function pgText(key) {
        const data = window.PromptRuntimeData || {};
        const fromRuntime = data && data.uiText && Object.prototype.hasOwnProperty.call(data.uiText, key) ? data.uiText[key] : null;
        return String(fromRuntime || PROMPT_GENERATOR_TEXT[key] || key);
    }

                setGeneratorStatus((label || pgText('text')) + pgText('copiedSuffix'));
                return true;
            }).catch(function (error) {
                console.warn('[generator][clipboard][browser-failed] ' + String(error && error.stack ? error.stack : error));
                return false;
            });
        }
        const temp = document.createElement('textarea');
        temp.value = value;
        temp.setAttribute('readonly', 'readonly');
        temp.style.position = 'fixed';
        temp.style.left = '-9999px';
        document.body.appendChild(temp);
        temp.select();
        let ok = false;
        try { ok = document.execCommand('copy'); } catch (error) { ok = false; }
        document.body.removeChild(temp);
        setGeneratorStatus(ok ? ((label || pgText('text')) + pgText('copiedSuffix')) : pgText('copyFailed'));
        return Promise.resolve(ok);
    }

    function escapeHtml(value) {
        return String(value || '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function decodeHtmlEntities(value) {
        const node = document.createElement('textarea');
        node.innerHTML = String(value || '');
        return String(node.value || '');
    }

    function applyEmojiTokens(value) {
        return String(value || '')
            .replace(/-eggplant-emoji-?/g, '🍆')
            .replace(/-warning-emoji-?/g, '⚠️')
            .replace(/-check-emoji-?/g, '✅');
    }

    function substituteTemplate(row, template) {
        let built = String(template || '');
        if (!row) {
            return trimValue(built);
        }
        row.querySelectorAll('.workflow-inline-input').forEach(function (node, index) {
            built = built.split('{{input' + String(index) + '}}').join(trimValue(node.value || ''));
        });
        return trimValue(built.replace(/\s+/g, ' '));
    }

    function plainTextFromTemplate(text) {
        let value = applyEmojiTokens(String(text || ''));
        value = value.replace(/\{\{input(?::([^}]+))?\}\}/g, function (_, placeholder) {
            return trimValue(placeholder || '');
        });
        value = value.replace(/\*\*([^*]+)\*\*/g, '$1');
        value = value.replace(/(^|[^*])\*([^*]+)\*(?!\*)/g, '$1$2');
        value = value.replace(/(^|[^_])_([^_]+)_(?!_)/g, '$1$2');
        value = value.replace(/`([^`]+)`/g, '$1');
        value = value.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '$1 ($2)');
        value = value.replace(/\\-/g, '-');
        value = decodeHtmlEntities(value);
        return trimValue(value.replace(/\s+/g, ' '));
    }

    function inlineTemplateToHtml(text) {
        let inputIndex = 0;
        const prepared = applyEmojiTokens(decodeHtmlEntities(String(text || '')));
        return escapeHtml(prepared).replace(/\{\{input(?::([^}]+))?\}\}/g, function (_, placeholder) {
            const label = escapeHtml(placeholder || '');
            const rendered = '<input type="text" class="workflow-inline-input" placeholder="' + label + '" data-inline-index="' + String(inputIndex) + '">';
            inputIndex += 1;
            return rendered;
        });
    }

    function renderInlineCheckboxToken(checked) {
        return '<label class="workflow-inline-checkbox-token"><input type="checkbox" disabled' + (checked ? ' checked' : '') + '></label>';
    }

    function renderInlineRadioToken(checked) {
        return '<label class="workflow-inline-checkbox-token"><input type="radio" disabled name="workflow-inline-radio-group"' + (checked ? ' checked' : '') + '></label>';
    }

    function renderInlineMarkdown(text) {
        let html = inlineTemplateToHtml(text);
        html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
        html = html.replace(/(^|[^*])\*([^*]+)\*(?!\*)/g, '$1<em>$2</em>');
        html = html.replace(/(^|[^_])_([^_]+)_(?!_)/g, '$1<em>$2</em>');
        html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
        html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2">$1</a>');
        html = html.replace(/(^|\s)\[(x|X| )?\](?=\s|$)/g, function (_, prefix, marker) {
            return prefix + renderInlineCheckboxToken(String(marker || '').toLowerCase() === 'x');
        });
        html = html.replace(/(^|\s)\[(o|O|0)\](?=\s|$)/g, function (_, prefix) {
            return prefix + renderInlineRadioToken(false);
        });
        return html;
    }

    function normalizeWorkflowSource(text) {
        return String(text || '')
            .replace(/\r\n/g, '\n')
            .replace(/\/\*[\s\S]*?\*\//g, '\n');
    }

    function parseWorkflowSource(text) {
        const sourceText = normalizeWorkflowSource(text);
        const lines = sourceText.split('\n');
        const blocks = [];
        let titleSet = false;
        for (let index = 0; index < lines.length; index += 1) {
            const rawLine = String(lines[index] || '');
            const trimmed = trimValue(rawLine);
            if (!trimmed) {
                blocks.push({ type: 'blank' });
                continue;
            }
            if (/^#\s+/.test(trimmed) && !titleSet) {
                titleSet = true;
                blocks.push({ type: 'title', level: 1, text: trimmed.replace(/^#\s+/, '') });
                continue;
            }
            if (/^(\/\/|#)/.test(trimmed)) {
                continue;
            }
            if (trimmed === '--' || /^---+$/.test(trimmed)) {
                blocks.push({ type: 'hr' });
                continue;
            }
            if (/^\[table\]$/i.test(trimmed)) {
                const rows = [];
                index += 1;
                while (index < lines.length && !/^\[\/table\]$/i.test(trimValue(lines[index]))) {
                    const rowText = trimValue(lines[index]);
                    if (rowText) {
                        let cells = [];
                        if (/^--/.test(rowText)) {
                            cells = ['', rowText.replace(/^--+/, '').trim()];
                        } else {
                            cells = rowText
                                .split(/(?=-cell\s+)/)
                                .map(function (cell) { return trimValue(cell).replace(/^-cell\s+/, ''); })
                                .filter(Boolean);
                        }
                        rows.push(cells.length ? cells : [rowText]);
                    }
                    index += 1;
                }
                blocks.push({ type: 'table', rows: rows });
                continue;
            }
            if (/^(?:\[(?:x|X| )?\]\s*)+$/.test(trimmed)) {
                blocks.push({ type: 'inline-checkboxes', text: trimmed });
                continue;
            }
            if (/^(?:\[(?:o|O|0)\]\s*)+$/.test(trimmed)) {
                blocks.push({ type: 'inline-radios', text: trimmed });
                continue;
            }
            const radioItems = extractChoiceItems(trimmed, /\[(o|O|0)\]\s*/g);
            if (radioItems.length && /^(?:[-*+]\s+)?\[(?:o|O|0)\]/.test(trimmed)) {
                radioItems.forEach(function (item) {
                    blocks.push({ type: 'radio-item', checked: false, template: item.label });
                });
                continue;
            }
            const checkboxMatch = trimmed.match(/^(?:[-*+]\s+)?\[(x|X| )?\]\s*(.+)$/);
            if (checkboxMatch) {
                blocks.push({
                    type: 'checkbox',
                    checked: (checkboxMatch[1] || '').toLowerCase() === 'x',
                    template: applyEmojiTokens(checkboxMatch[2])
                });
                continue;
            }
            const headingMatch = trimmed.match(/^(#{1,6})\s+(.+)$/);
            if (headingMatch) {
                blocks.push({ type: 'heading', level: Math.min(6, headingMatch[1].length), text: headingMatch[2] });
                continue;
            }
            const quoteMatch = trimmed.match(/^>\s+(.+)$/);
            if (quoteMatch) {
                blocks.push({ type: 'quote', text: quoteMatch[1] });
                continue;
            }
            const bulletMatch = trimmed.match(/^[-*+]\s+(.+)$/);
            if (bulletMatch) {
                blocks.push({ type: 'bullet', text: bulletMatch[1] });
                continue;
            }
            const orderedMatch = trimmed.match(/^\d+\.\s+(.+)$/);
            if (orderedMatch) {
                blocks.push({ type: 'ordered', text: orderedMatch[1] });
                continue;
            }
            const boldMatch = trimmed.match(/^\[b\]\s*(.+)$/i);
            if (boldMatch) {
                blocks.push({ type: 'bold', text: boldMatch[1] });
                continue;
            }
            blocks.push({ type: 'paragraph', text: applyEmojiTokens(trimmed) });
        }
        return blocks;
    }

    function collectInstructionLines() {
        const lines = [];
        document.querySelectorAll('.workflow-prompt-control').forEach(function (node) {
            if (!node.checked || node.disabled) {
                return;
            }
            const row = node.closest('.workflow-control-row');
            const template = node.getAttribute('data-template') || (row ? row.getAttribute('data-template') : '') || '';
            const built = substituteTemplate(row, template);
            if (built) {
                lines.push(built);
            }
        });
        const extraInstruction = trimValue(getValue('extraInstruction'));
        if (extraInstruction) {
            lines.push(extraInstruction);
        }
        return lines;
    }

    function collectPassiveWorkflowLines() {
        const blocks = Array.isArray(window.__workflowBlocks) ? window.__workflowBlocks : [];
        const lines = [];
        blocks.forEach(function (block) {
            if (!block || !block.type) {
                return;
            }
            if (block.type === 'table') {
                (block.rows || []).forEach(function (row) {
                    const pieces = (row || []).map(function (cell) {
                        return plainTextFromTemplate(cell);
                    }).filter(Boolean);
                    if (pieces.length) {
                        lines.push(pieces.join(' | '));
                    }
                });
                return;
            }
            if (['paragraph', 'bullet', 'ordered', 'quote', 'bold', 'heading'].indexOf(block.type) >= 0) {
                const value = plainTextFromTemplate(block.text || '');
                if (value) {
                    lines.push(value);
                }
            }
        });
        return lines;
    }

    function buildPromptNow() {
        return withErrorBoundary('buildPromptNow', function () {
            const task = trimValue(getValue('userTask'));
            const baseContext = trimValue(getValue('baseContext'));
            const doctypeText = trimValue(getValue('doctypeText'));
            const promptTitle = trimValue(getValue('promptTitle'));
            const instructionLines = collectInstructionLines();
            const passiveLines = collectPassiveWorkflowLines();
            const previewLines = [];
            const blocks = [];

            if (task) {
                blocks.push(task);
            }
            if (instructionLines.length) {
                blocks.push(pgText('instructionsBlock') + ':\n- ' + instructionLines.join('\n- '));
                previewLines.push(pgText('instructionsBlock') + ':');
                instructionLines.forEach(function (line) { previewLines.push('- ' + line); });
            }
            if (passiveLines.length) {
                blocks.push(pgText('workflowMarkdownBlock') + ':\n- ' + passiveLines.join('\n- '));
                if (previewLines.length) {
                    previewLines.push('');
                }
                previewLines.push(pgText('workflowMarkdownBlock') + ':');
                passiveLines.forEach(function (line) { previewLines.push('- ' + line); });
            }
            if (baseContext) {
                blocks.push(baseContext);
            }
            if (doctypeText) {
                blocks.push(pgText('doctypeBlock') + ':\n' + doctypeText);
            }

            const finalPrompt = blocks.join('\n\n');
            setValue('openedPrompt', finalPrompt);
            setValue('previewList', previewLines.join('\n'));
            if (!promptTitle) {
                setValue('promptTitle', pgText('generatedPrompt'));
            }
            setGeneratorStatus(pgText('ready') + ' ' + pgText('lines') + '=' + String(instructionLines.length + passiveLines.length) + ' ' + pgText('chars') + '=' + String(finalPrompt.length));
            bridgePrint('[generator][rebuilt] lines=' + String(instructionLines.length + passiveLines.length) + ' chars=' + String(finalPrompt.length));
            return finalPrompt;
        });
    }

    function buildGeneratePayload(generatedPrompt) {
        const data = getRuntimeData();
        return {
            title: trimValue(getValue('promptTitle')),
            task: trimValue(getValue('userTask')),
            generated_prompt: String(generatedPrompt || getValue('openedPrompt') || ''),
            instruction_lines: String(getValue('previewList') || ''),
            workflowSlug: String(data.workflowSlug || ''),
            workflowSourcePath: String(data.workflowSourcePath || ''),
            workflowEditorSourcePath: String(data.workflowEditorSourcePath || ''),
            workflowEditorMd5: String(data.workflowEditorMd5 || '')
        };
    }

    function generateButtonAction() {
        return withErrorBoundary('generateButtonAction', function () {
            const generated = buildPromptNow();
            const payload = buildGeneratePayload(generated);
            if (window.qtBridge && typeof window.qtBridge.generatePromptFromWeb === 'function') {
                bridgePrint('[generator][generate-click] bridge save/regenerate requested source=' + payload.workflowSourcePath + ' chars=' + String(payload.generated_prompt.length));
                window.qtBridge.generatePromptFromWeb(JSON.stringify(payload));
                setGeneratorStatus(pgText('generateRequested'));
                return generated;
            }
            bridgePrint('[generator][generate-click] rebuilt locally; Qt bridge missing');
            setGeneratorStatus(pgText('regeneratedLocally'));
            return generated;
        });
    }

    function copyGeneratedPrompt() {
        return withErrorBoundary('copyGeneratedPrompt', function () {
            const text = getValue('openedPrompt');
            if (!trimValue(text)) {
                setGeneratorStatus(pgText('nothingToCopy'));
                return null;
            }
            return copyTextWithFallback(text, pgText('generatedPromptLabel'));
        });
    }

    function copyColorHex() {
        return withErrorBoundary('copyColorHex', function () {
            const color = applyGeneratedColor(getValue('quickColorHex') || (document.getElementById('quickColorPicker') || {}).value || '#8ecbff');
            return copyTextWithFallback(color, pgText('colorHexLabel'));
        });
    }

    function syncColorFromPicker() {
        return withErrorBoundary('syncColorFromPicker', function () {
            const picker = document.getElementById('quickColorPicker');
            applyGeneratedColor(picker ? picker.value : '#8ecbff');
        });
    }

    function syncColorFromHex() {
        return withErrorBoundary('syncColorFromHex', function () {
            const hex = getValue('quickColorHex');
            const normalized = normalizeHexColor(hex);
            if (normalized) {
                applyGeneratedColor(normalized);
            }
        });
    }

    function clearGeneratorSelections() {
        return withErrorBoundary('clearGeneratorSelections', function () {
            document.querySelectorAll('.workflow-prompt-control').forEach(function (node) {
                if (!node.disabled) {
                    node.checked = false;
                }
            });
            document.querySelectorAll('.workflow-inline-input').forEach(function (node) {
                node.value = '';
            });
            setValue('extraInstruction', '');
            return buildPromptNow();
        });
    }

    function renderWorkflowControls() {
        return withErrorBoundary('renderWorkflowControls', function () {
            const data = getRuntimeData();
            const sourceNode = document.getElementById('workflowSource');
            const sourceText = normalizeWorkflowSource(data.workflowSource || (sourceNode ? sourceNode.textContent : ''));
            if (sourceNode) {
                sourceNode.textContent = sourceText;
            }
            const container = document.getElementById('dynamicControls');
            if (!container) {
                return;
            }

            const blocks = Array.isArray(data.workflowBlocks) ? data.workflowBlocks : parseWorkflowSource(sourceText);
            window.__workflowBlocks = blocks;
            if (container.children.length && Array.isArray(data.workflowBlocks)) {
                return;
            }
            const htmlParts = [];
            let listMode = null;
            let radioGroupIndex = 0;

            function closeList() {
                if (listMode) {
                    htmlParts.push('</' + listMode + '>');
                    listMode = null;
                }
            }

            for (let index = 0; index < blocks.length; index += 1) {
                const block = blocks[index];
                if (!block || !block.type || block.type === 'blank' || block.type === 'title') {
                    closeList();
                    continue;
                }
                if (block.type === 'hr') {
                    closeList();
                    htmlParts.push('<hr class="workflow-hr">');
                    continue;
                }
                if (block.type === 'inline-checkboxes' || block.type === 'inline-radios') {
                    closeList();
                    htmlParts.push('<p class="workflow-inline-checkboxes">' + renderInlineMarkdown(block.text || '') + '</p>');
                    continue;
                }
                if (block.type === 'table') {
                    closeList();
                    const rows = (block.rows || []).map(function (row) {
                        const cells = (row || []).map(function (cell) {
                            return '<td>' + renderInlineMarkdown(cell) + '</td>';
                        }).join('');
                        return '<tr>' + cells + '</tr>';
                    }).join('');
                    if (rows) {
                        htmlParts.push('<table class="workflow-table">' + rows + '</table>');
                    }
                    continue;
                }
                if (block.type === 'checkbox') {
                    closeList();
                    htmlParts.push(
                        '<div class="workflow-control-block workflow-control-row" data-template="' + escapeHtml(block.template) + '">' +
                            '<label class="checkline">' +
                                '<input type="checkbox" class="workflow-prompt-control" data-template="' + escapeHtml(block.template) + '"' + (block.checked ? ' checked' : '') + '>' +
                                '<span>' + renderInlineMarkdown(block.template) + '</span>' +
                            '</label>' +
                        '</div>'
                    );
                    continue;
                }
                if (block.type === 'radio-item') {
                    closeList();
                    const groupName = 'workflow-radio-group-' + String(radioGroupIndex);
                    const radioItems = [];
                    while (index < blocks.length && blocks[index] && blocks[index].type === 'radio-item') {
                        radioItems.push(blocks[index]);
                        index += 1;
                    }
                    index -= 1;
                    htmlParts.push('<fieldset class="radio-group"><legend class="radio-group-label">Choose one</legend>');
                    radioItems.forEach(function (item) {
                        htmlParts.push(
                            '<div class="workflow-control-block workflow-control-row" data-template="' + escapeHtml(item.template) + '">' +
                                '<label class="checkline">' +
                                    '<input type="radio" name="' + escapeHtml(groupName) + '" class="workflow-prompt-control" data-template="' + escapeHtml(item.template) + '"' + (item.checked ? ' checked' : '') + '>' +
                                    '<span>' + renderInlineMarkdown(item.template) + '</span>' +
                                '</label>' +
                            '</div>'
                        );
                    });
                    htmlParts.push('</fieldset>');
                    radioGroupIndex += 1;
                    continue;
                }
                if (block.type === 'heading') {
                    closeList();
                    htmlParts.push('<h' + String(block.level || 2) + '>' + renderInlineMarkdown(block.text || '') + '</h' + String(block.level || 2) + '>');
                    continue;
                }
                if (block.type === 'quote') {
                    closeList();
                    htmlParts.push('<blockquote>' + renderInlineMarkdown(block.text || '') + '</blockquote>');
                    continue;
                }
                if (block.type === 'bullet') {
                    if (listMode !== 'ul') {
                        closeList();
                        listMode = 'ul';
                        htmlParts.push('<ul>');
                    }
                    htmlParts.push('<li>' + renderInlineMarkdown(block.text || '') + '</li>');
                    continue;
                }
                if (block.type === 'ordered') {
                    if (listMode !== 'ol') {
                        closeList();
                        listMode = 'ol';
                        htmlParts.push('<ol>');
                    }
                    htmlParts.push('<li>' + renderInlineMarkdown(block.text || '') + '</li>');
                    continue;
                }
                if (block.type === 'bold') {
                    closeList();
                    htmlParts.push('<p class="workflow-format-bold">' + renderInlineMarkdown(block.text || '') + '</p>');
                    continue;
                }
                closeList();
                htmlParts.push('<p>' + renderInlineMarkdown(block.text || '') + '</p>');
            }

            closeList();
            if (!htmlParts.length) {
                // Keep this area empty when the workflow produces no controls.
            }
            container.innerHTML = htmlParts.join('');
        });
    }

    function bindEvents() {
        if (hasJQuery()) {
            const $ = window.jQuery;
            $('#copyPromptButton').off('click.prompt').on('click.prompt', copyGeneratedPrompt);
            $('#rebuildPromptButton').off('click.prompt').on('click.prompt', generateButtonAction);
            $('#copyColorButton').off('click.prompt').on('click.prompt', copyColorHex);
            $('#quickColorPicker').off('input.promptColor change.promptColor').on('input.promptColor change.promptColor', syncColorFromPicker);
            $('#quickColorHex').off('input.promptColor change.promptColor').on('input.promptColor change.promptColor', syncColorFromHex);
            $(document).off('.promptGenerator');
            $(document).on('input.promptGenerator change.promptGenerator', '#promptTitle, #userTask, #baseContext, #doctypeText, #extraInstruction, .workflow-inline-input, .workflow-prompt-control', buildPromptNow);
            return;
        }
        [
            ['copyPromptButton', copyGeneratedPrompt],
            ['rebuildPromptButton', generateButtonAction],
            ['copyColorButton', copyColorHex],
            ['quickColorPicker', syncColorFromPicker],
            ['quickColorHex', syncColorFromHex]
        ].forEach(function (pair) {
            const node = document.getElementById(pair[0]);
            if (node) {
                node.addEventListener(pair[0] === 'quickColorPicker' || pair[0] === 'quickColorHex' ? 'input' : 'click', pair[1]);
                if (pair[0] === 'quickColorPicker' || pair[0] === 'quickColorHex') {
                    node.addEventListener('change', pair[1]);
                }
            }
        });
        ['promptTitle', 'userTask', 'baseContext', 'doctypeText', 'extraInstruction'].forEach(function (id) {
            const node = document.getElementById(id);
            if (node) {
                node.addEventListener('input', buildPromptNow);
            }
        });
        document.querySelectorAll('.workflow-inline-input').forEach(function (node) {
            node.addEventListener('input', buildPromptNow);
        });
        document.querySelectorAll('.workflow-prompt-control').forEach(function (node) {
            node.addEventListener('change', buildPromptNow);
        });
    }

    function initializePromptGenerator() {
        return withErrorBoundary('initializePromptGenerator', function () {
            const data = getRuntimeData();
            bridgePrint('[generator][init] href=' + String(window.location.href || '') + ' jquery=' + (hasJQuery() ? window.jQuery.fn.jquery : 'missing'));
            setValue('promptTitle', data.promptTitle || '');
            setValue('userTask', data.task || '');
            setValue('baseContext', data.context || '');
            setValue('doctypeText', data.doctypeText || '');
            setText('doctypeName', data.doctypeName || '');
            renderWorkflowControls();
            bindEvents();
            buildPromptNow();
            applyGeneratedColor(getValue('quickColorHex') || '#8ecbff');
            setGeneratorStatus(pgText('ready'));
        });
    }

    window.copyGeneratedPrompt = copyGeneratedPrompt;
    window.clearGeneratorSelections = clearGeneratorSelections;
    window.buildPromptNow = buildPromptNow;
    window.generateButtonAction = generateButtonAction;

    if (hasJQuery()) {
        window.jQuery(initializePromptGenerator);
    } else if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initializePromptGenerator);
    } else {
        initializePromptGenerator();
    }
})();
