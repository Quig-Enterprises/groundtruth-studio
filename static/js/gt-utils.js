// Groundtruth Studio - Global Utilities
// Override alert() to add debug logging
(function() {
    var _originalAlert = window.alert;

    window.alert = function(message) {
        var timestamp = new Date().toISOString();
        var page = window.location.pathname;
        var stack = new Error().stack || '';

        // Extract caller info from stack (skip this function and the Error constructor)
        var callerLine = '';
        var stackLines = stack.split('\n');
        for (var i = 1; i < stackLines.length; i++) {
            var line = stackLines[i].trim();
            if (line && !line.includes('gt-utils.js')) {
                callerLine = line;
                break;
            }
        }

        var isError = typeof message === 'string' &&
            (message.startsWith('Error') || message.startsWith('Failed') ||
             message.includes('error') || message.includes('failed'));

        var logEntry = {
            timestamp: timestamp,
            page: page,
            message: message,
            caller: callerLine,
            type: isError ? 'error' : 'info'
        };

        if (isError) {
            console.error('[GT Alert]', logEntry);
        } else {
            console.info('[GT Alert]', logEntry);
        }

        // Fire-and-forget log to server
        try {
            navigator.sendBeacon('/api/client-log', JSON.stringify(logEntry));
        } catch (e) {
            // Silently ignore if endpoint doesn't exist
        }

        _originalAlert.call(window, message);
    };
})();
