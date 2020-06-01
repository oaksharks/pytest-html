# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

from base64 import b64encode, b64decode
from collections import OrderedDict
from os.path import isfile
import datetime
import json
import os
import pkg_resources
import time
import bisect
import warnings
import re

from html import escape
import pytest

try:
    from ansi2html import Ansi2HTMLConverter, style

    ANSI = True
except ImportError:
    # ansi2html is not installed
    ANSI = False

from py.xml import html, raw

from . import extras
from . import __version__, __pypi_url__


def pytest_addhooks(pluginmanager):
    from . import hooks

    pluginmanager.add_hookspecs(hooks)


def pytest_addoption(parser):
    group = parser.getgroup("terminal reporting")
    group.addoption(
        "--html",
        action="store",
        dest="htmlpath",
        metavar="path",
        default=None,
        help="create html report file at given path.",
    )
    group.addoption(
        "--self-contained-html",
        action="store_true",
        help="create a self-contained html file containing all "
        "necessary styles, scripts, and images - this means "
        "that the report may not render or function where CSP "
        "restrictions are in place (see "
        "https://developer.mozilla.org/docs/Web/Security/CSP)",
    )
    group.addoption(
        "--css",
        action="append",
        metavar="path",
        default=[],
        help="append given css file content to report style file.",
    )
    parser.addini(
        "render_collapsed",
        type="bool",
        default=False,
        help="Open the report with all rows collapsed. Useful for very large reports",
    )


def pytest_configure(config):
    htmlpath = config.getoption("htmlpath")
    if htmlpath:
        for csspath in config.getoption("css"):
            if not os.path.exists(csspath):
                raise IOError(f"No such file or directory: '{csspath}'")
        if not hasattr(config, "slaveinput"):
            # prevent opening htmlpath on slave nodes (xdist)
            config._html = HTMLReport(htmlpath, config)
            config.pluginmanager.register(config._html)


def pytest_unconfigure(config):
    html = getattr(config, "_html", None)
    if html:
        del config._html
        config.pluginmanager.unregister(html)


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when == "call":
        fixture_extras = getattr(item.config, "extras", [])
        plugin_extras = getattr(report, "extra", [])
        report.extra = fixture_extras + plugin_extras


@pytest.fixture
def extra(pytestconfig):
    """Add details to the HTML reports.

    .. code-block:: python

        import pytest_html
        def test_foo(extra):
            extra.append(pytest_html.extras.url('http://www.example.com/'))
    """
    pytestconfig.extras = []
    yield pytestconfig.extras
    del pytestconfig.extras[:]


def data_uri(content, mime_type="text/plain", charset="utf-8"):
    data = b64encode(content.encode(charset)).decode("ascii")
    return f"data:{mime_type};charset={charset};base64,{data}"


class HTMLReport:
    def __init__(self, logfile, config):
        logfile = os.path.expanduser(os.path.expandvars(logfile))
        self.logfile = os.path.abspath(logfile)
        self.test_logs = []
        self.title = os.path.basename(self.logfile)
        self.results = []
        self.errors = self.failed = 0
        self.passed = self.skipped = 0
        self.xfailed = self.xpassed = 0
        has_rerun = config.pluginmanager.hasplugin("rerunfailures")
        self.rerun = 0 if has_rerun else None
        self.self_contained = config.getoption("self_contained_html")
        self.config = config

    class TestResult:
        def __init__(self, outcome, report, logfile, config):
            self.test_id = report.nodeid.encode("utf-8").decode("unicode_escape")
            if getattr(report, "when", "call") != "call":
                self.test_id = "::".join([report.nodeid, report.when])
            self.time = getattr(report, "duration", 0.0)
            self.outcome = outcome
            self.additional_html = []
            self.links_html = []
            self.self_contained = config.getoption("self_contained_html")
            self.logfile = logfile
            self.config = config
            self.row_table = self.row_extra = None

            test_index = hasattr(report, "rerun") and report.rerun + 1 or 0

            for extra_index, extra in enumerate(getattr(report, "extra", [])):
                self.append_extra_html(extra, extra_index, test_index)

            self.append_log_html(report, self.additional_html)

            cells = [
                html.td(self.outcome, class_="col-result"),
                html.td(self.test_id, class_="col-name"),
                html.td(f"{self.time:.2f}", class_="col-duration"),
                html.td(self.links_html, class_="col-links"),
            ]

            self.config.hook.pytest_html_results_table_row(report=report, cells=cells)

            self.config.hook.pytest_html_results_table_html(
                report=report, data=self.additional_html
            )

            if len(cells) > 0:
                tr_class = None
                if self.config.getini("render_collapsed"):
                    tr_class = "collapsed"
                self.row_table = html.tr(cells)
                self.row_extra = html.tr(
                    html.td(self.additional_html, class_="extra", colspan=len(cells)),
                    class_=tr_class,
                )

        def __lt__(self, other):
            order = (
                "Error",
                "Failed",
                "Rerun",
                "XFailed",
                "XPassed",
                "Skipped",
                "Passed",
            )
            return order.index(self.outcome) < order.index(other.outcome)

        def create_asset(
            self, content, extra_index, test_index, file_extension, mode="w"
        ):
            # 255 is the common max filename length on various filesystems
            asset_file_name = "{}_{}_{}.{}".format(
                re.sub(r"[^\w\.]", "_", self.test_id),
                str(extra_index),
                str(test_index),
                file_extension,
            )[-255:]
            asset_path = os.path.join(
                os.path.dirname(self.logfile), "assets", asset_file_name
            )

            if not os.path.exists(os.path.dirname(asset_path)):
                os.makedirs(os.path.dirname(asset_path))

            relative_path = f"assets/{asset_file_name}"

            kwargs = {"encoding": "utf-8"} if "b" not in mode else {}
            with open(asset_path, mode, **kwargs) as f:
                f.write(content)
            return relative_path

        def append_extra_html(self, extra, extra_index, test_index):
            href = None
            if extra.get("format") == extras.FORMAT_IMAGE:
                self._append_image(extra, extra_index, test_index)

            elif extra.get("format") == extras.FORMAT_HTML:
                self.additional_html.append(html.div(raw(extra.get("content"))))

            elif extra.get("format") == extras.FORMAT_JSON:
                content = json.dumps(extra.get("content"))
                if self.self_contained:
                    href = data_uri(content, mime_type=extra.get("mime_type"))
                else:
                    href = self.create_asset(
                        content, extra_index, test_index, extra.get("extension")
                    )

            elif extra.get("format") == extras.FORMAT_TEXT:
                content = extra.get("content")
                if isinstance(content, bytes):
                    content = content.decode("utf-8")
                if self.self_contained:
                    href = data_uri(content)
                else:
                    href = self.create_asset(
                        content, extra_index, test_index, extra.get("extension")
                    )

            elif extra.get("format") == extras.FORMAT_URL:
                href = extra.get("content")

            elif extra.get("format") == extras.FORMAT_VIDEO:
                self._append_video(extra, extra_index, test_index)

            if href is not None:
                self.links_html.append(
                    html.a(
                        extra.get("name"),
                        class_=extra.get("format"),
                        href=href,
                        target="_blank",
                    )
                )
                self.links_html.append(" ")

        def append_log_html(self, report, additional_html):
            log = html.div(class_="log")
            if report.longrepr:
                for line in report.longreprtext.splitlines():
                    separator = line.startswith("_ " * 10)
                    if separator:
                        log.append(line[:80])
                    else:
                        exception = line.startswith("E   ")
                        if exception:
                            log.append(html.span(raw(escape(line)), class_="error"))
                        else:
                            log.append(raw(escape(line)))
                    log.append(html.br())

            for section in report.sections:
                header, content = map(escape, section)
                log.append(f" {header:-^80} ")
                log.append(html.br())
                if ANSI:
                    converter = Ansi2HTMLConverter(inline=False, escaped=False)
                    content = converter.convert(content, full=False)
                log.append(raw(content))
                log.append(html.br())

            if len(log) == 0:
                log = html.div(class_="empty log")
                log.append("No log output captured.")
            additional_html.append(log)

        def _make_media_html_div(
            self, extra, extra_index, test_index, base_extra_string, base_extra_class
        ):
            content = extra.get("content")
            try:
                is_uri_or_path = content.startswith(("file", "http")) or isfile(content)
            except ValueError:
                # On Windows, os.path.isfile throws this exception when
                # passed a b64 encoded image.
                is_uri_or_path = False
            if is_uri_or_path:
                if self.self_contained:
                    warnings.warn(
                        "Self-contained HTML report "
                        "includes link to external "
                        f"resource: {content}"
                    )

                html_div = html.a(
                    raw(base_extra_string.format(extra.get("content"))), href=content
                )
            elif self.self_contained:
                src = f"data:{extra.get('mime_type')};base64,{content}"
                html_div = raw(base_extra_string.format(src))
            else:
                content = b64decode(content.encode("utf-8"))
                href = src = self.create_asset(
                    content, extra_index, test_index, extra.get("extension"), "wb"
                )
                html_div = html.a(
                    raw(base_extra_string.format(src)),
                    class_=base_extra_class,
                    target="_blank",
                    href=href,
                )
            return html_div

        def _append_image(self, extra, extra_index, test_index):
            image_base = '<img src="{}"/>'
            html_div = self._make_media_html_div(
                extra, extra_index, test_index, image_base, "image"
            )
            self.additional_html.append(html.div(html_div, class_="image"))

        def _append_video(self, extra, extra_index, test_index):
            video_base = '<video controls><source src="{}" type="video/mp4"></video>'
            html_div = self._make_media_html_div(
                extra, extra_index, test_index, video_base, "video"
            )
            self.additional_html.append(html.div(html_div, class_="video"))

    def _appendrow(self, outcome, report):
        result = self.TestResult(outcome, report, self.logfile, self.config)
        if result.row_table is not None:
            index = bisect.bisect_right(self.results, result)
            self.results.insert(index, result)
            tbody = html.tbody(
                result.row_table,
                class_="{} results-table-row".format(result.outcome.lower()),
            )
            if result.row_extra is not None:
                tbody.append(result.row_extra)
            self.test_logs.insert(index, tbody)

    def append_passed(self, report):
        if report.when == "call":
            if hasattr(report, "wasxfail"):
                self.xpassed += 1
                self._appendrow("XPassed", report)
            else:
                self.passed += 1
                self._appendrow("Passed", report)

    def append_failed(self, report):
        if getattr(report, "when", None) == "call":
            if hasattr(report, "wasxfail"):
                # pytest < 3.0 marked xpasses as failures
                self.xpassed += 1
                self._appendrow("XPassed", report)
            else:
                self.failed += 1
                self._appendrow("Failed", report)
        else:
            self.errors += 1
            self._appendrow("Error", report)

    def append_skipped(self, report):
        if hasattr(report, "wasxfail"):
            self.xfailed += 1
            self._appendrow("XFailed", report)
        else:
            self.skipped += 1
            self._appendrow("Skipped", report)

    def append_other(self, report):
        # For now, the only "other" the plugin give support is rerun
        self.rerun += 1
        self._appendrow("Rerun", report)

    def _generate_report(self, session):
        suite_stop_time = time.time()
        suite_time_delta = suite_stop_time - self.suite_start_time
        numtests = self.passed + self.failed + self.xpassed + self.xfailed
        generated = datetime.datetime.now()

        self.style_css = \
"""
body {
	font-family: Helvetica, Arial, sans-serif;
	font-size: 12px;
	/* do not increase min-width as some may use split screens */
	min-width: 800px;
	color: #999;
}

h1 {
	font-size: 24px;
	color: black;
}

h2 {
	font-size: 16px;
	color: black;
}

p {
    color: black;
}

a {
	color: #999;
}

table {
	border-collapse: collapse;
}

/******************************
 * SUMMARY INFORMATION
 ******************************/

#environment td {
	padding: 5px;
	border: 1px solid #E6E6E6;
}

#environment tr:nth-child(odd) {
	background-color: #f6f6f6;
}

/******************************
 * TEST RESULT COLORS
 ******************************/
span.passed, .passed .col-result {
	color: green;
}
span.skipped, span.xfailed, span.rerun, .skipped .col-result, .xfailed .col-result, .rerun .col-result {
	color: orange;
}
span.error, span.failed, span.xpassed, .error .col-result, .failed .col-result, .xpassed .col-result  {
	color: red;
}


/******************************
 * RESULTS TABLE
 *
 * 1. Table Layout
 * 2. Extra
 * 3. Sorting items
 *
 ******************************/

/*------------------
 * 1. Table Layout
 *------------------*/

#results-table {
	border: 1px solid #e6e6e6;
	color: #999;
	font-size: 12px;
	width: 100%
}

#results-table th, #results-table td {
	padding: 5px;
	border: 1px solid #E6E6E6;
	text-align: left
}
#results-table th {
	font-weight: bold
}

/*------------------
 * 2. Extra
 *------------------*/

.log:only-child {
	height: inherit
}
.log {
	background-color: #e6e6e6;
	border: 1px solid #e6e6e6;
	color: black;
	display: block;
	font-family: "Courier New", Courier, monospace;
	height: 230px;
	overflow-y: scroll;
	padding: 5px;
	white-space: pre-wrap
}
div.image {
	border: 1px solid #e6e6e6;
	float: right;
	height: 240px;
	margin-left: 5px;
	overflow: hidden;
	width: 320px
}
div.image img {
	width: 320px
}
div.video {
	border: 1px solid #e6e6e6;
	float: right;
	height: 240px;
	margin-left: 5px;
	overflow: hidden;
	width: 320px
}
div.video video {
	overflow: hidden;
	width: 320px;
    height: 240px;
}
.collapsed {
	display: none;
}
.expander::after {
	content: " (show details)";
	color: #BBB;
	font-style: italic;
	cursor: pointer;
}
.collapser::after {
	content: " (hide details)";
	color: #BBB;
	font-style: italic;
	cursor: pointer;
}

/*------------------
 * 3. Sorting items
 *------------------*/
.sortable {
	cursor: pointer;
}

.sort-icon {
	font-size: 0px;
	float: left;
	margin-right: 5px;
	margin-top: 5px;
	/*triangle*/
	width: 0;
	height: 0;
	border-left: 8px solid transparent;
	border-right: 8px solid transparent;
}

.inactive .sort-icon {
	/*finish triangle*/
	border-top: 8px solid #E6E6E6;
}

.asc.active .sort-icon {
	/*finish triangle*/
	border-bottom: 8px solid #999;
}

.desc.active .sort-icon {
	/*finish triangle*/
	border-top: 8px solid #999;
}
"""

        if ANSI:
            ansi_css = [
                "\n/******************************",
                " * ANSI2HTML STYLES",
                " ******************************/\n",
            ]
            ansi_css.extend([str(r) for r in style.get_styles()])
            self.style_css += "\n".join(ansi_css)

        # <DF> Add user-provided CSS
        for path in self.config.getoption("css"):
            self.style_css += "\n/******************************"
            self.style_css += "\n * CUSTOM CSS"
            self.style_css += f"\n * {path}"
            self.style_css += "\n ******************************/\n\n"
            with open(path, "r") as f:
                self.style_css += f.read()

        css_href = "assets/style.css"
        html_css = html.link(href=css_href, rel="stylesheet", type="text/css")
        if self.self_contained:
            html_css = html.style(raw(self.style_css))

        head = html.head(
            html.meta(charset="utf-8"), html.title("Test Report"), html_css
        )

        class Outcome:
            def __init__(
                self, outcome, total=0, label=None, test_result=None, class_html=None
            ):
                self.outcome = outcome
                self.label = label or outcome
                self.class_html = class_html or outcome
                self.total = total
                self.test_result = test_result or outcome

                self.generate_checkbox()
                self.generate_summary_item()

            def generate_checkbox(self):
                checkbox_kwargs = {"data-test-result": self.test_result.lower()}
                if self.total == 0:
                    checkbox_kwargs["disabled"] = "true"

                self.checkbox = html.input(
                    type="checkbox",
                    checked="true",
                    onChange="filter_table(this)",
                    name="filter_checkbox",
                    class_="filter",
                    hidden="true",
                    **checkbox_kwargs,
                )

            def generate_summary_item(self):
                self.summary_item = html.span(
                    f"{self.total} {self.label}", class_=self.class_html
                )

        outcomes = [
            Outcome("passed", self.passed),
            Outcome("skipped", self.skipped),
            Outcome("failed", self.failed),
            Outcome("error", self.errors, label="errors"),
            Outcome("xfailed", self.xfailed, label="expected failures"),
            Outcome("xpassed", self.xpassed, label="unexpected passes"),
        ]

        if self.rerun is not None:
            outcomes.append(Outcome("rerun", self.rerun))

        summary = [
            html.p(f"{numtests} tests ran in {suite_time_delta:.2f} seconds. "),
            html.p(
                "(Un)check the boxes to filter the results.",
                class_="filter",
                hidden="true",
            ),
        ]

        for i, outcome in enumerate(outcomes, start=1):
            summary.append(outcome.checkbox)
            summary.append(outcome.summary_item)
            if i < len(outcomes):
                summary.append(", ")

        cells = [
            html.th("Result", class_="sortable result initial-sort", col="result"),
            html.th("Test", class_="sortable", col="name"),
            html.th("Duration", class_="sortable numeric", col="duration"),
            html.th("Links"),
        ]
        session.config.hook.pytest_html_results_table_header(cells=cells)

        results = [
            html.h2("Results"),
            html.table(
                [
                    html.thead(
                        html.tr(cells),
                        html.tr(
                            [
                                html.th(
                                    "No results found. Try to check the filters",
                                    colspan=len(cells),
                                )
                            ],
                            id="not-found-message",
                            hidden="true",
                        ),
                        id="results-table-head",
                    ),
                    self.test_logs,
                ],
                id="results-table",
            ),
        ]

        main_js = \
"""
/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this file,
 * You can obtain one at http://mozilla.org/MPL/2.0/. */


function toArray(iter) {
    if (iter === null) {
        return null;
    }
    return Array.prototype.slice.call(iter);
}

function find(selector, elem) {
    if (!elem) {
        elem = document;
    }
    return elem.querySelector(selector);
}

function find_all(selector, elem) {
    if (!elem) {
        elem = document;
    }
    return toArray(elem.querySelectorAll(selector));
}

function sort_column(elem) {
    toggle_sort_states(elem);
    var colIndex = toArray(elem.parentNode.childNodes).indexOf(elem);
    var key;
    if (elem.classList.contains('numeric')) {
        key = key_num;
    } else if (elem.classList.contains('result')) {
        key = key_result;
    } else {
        key = key_alpha;
    }
    sort_table(elem, key(colIndex));
}

function show_all_extras() {
    find_all('.col-result').forEach(show_extras);
}

function hide_all_extras() {
    find_all('.col-result').forEach(hide_extras);
}

function show_extras(colresult_elem) {
    var extras = colresult_elem.parentNode.nextElementSibling;
    var expandcollapse = colresult_elem.firstElementChild;
    extras.classList.remove("collapsed");
    expandcollapse.classList.remove("expander");
    expandcollapse.classList.add("collapser");
}

function hide_extras(colresult_elem) {
    var extras = colresult_elem.parentNode.nextElementSibling;
    var expandcollapse = colresult_elem.firstElementChild;
    extras.classList.add("collapsed");
    expandcollapse.classList.remove("collapser");
    expandcollapse.classList.add("expander");
}

function show_filters() {
    var filter_items = document.getElementsByClassName('filter');
    for (var i = 0; i < filter_items.length; i++)
        filter_items[i].hidden = false;
}

function add_collapse() {
    // Add links for show/hide all
    var resulttable = find('table#results-table');
    var showhideall = document.createElement("p");
    showhideall.innerHTML = '<a href="javascript:show_all_extras()">Show all details</a> / ' +
                            '<a href="javascript:hide_all_extras()">Hide all details</a>';
    resulttable.parentElement.insertBefore(showhideall, resulttable);

    // Add show/hide link to each result
    find_all('.col-result').forEach(function(elem) {
        var collapsed = get_query_parameter('collapsed') || 'Passed';
        var extras = elem.parentNode.nextElementSibling;
        var expandcollapse = document.createElement("span");
        if (extras.classList.contains("collapsed")) {
            expandcollapse.classList.add("expander")
        } else if (collapsed.includes(elem.innerHTML)) {
            extras.classList.add("collapsed");
            expandcollapse.classList.add("expander");
        } else {
            expandcollapse.classList.add("collapser");
        }
        elem.appendChild(expandcollapse);

        elem.addEventListener("click", function(event) {
            if (event.currentTarget.parentNode.nextElementSibling.classList.contains("collapsed")) {
                show_extras(event.currentTarget);
            } else {
                hide_extras(event.currentTarget);
            }
        });
    })
}

function get_query_parameter(name) {
    var match = RegExp('[?&]' + name + '=([^&]*)').exec(window.location.search);
    return match && decodeURIComponent(match[1].replace(/\+/g, ' '));
}

function init () {
    reset_sort_headers();

    add_collapse();

    show_filters();

    sort_column(find('.initial-sort'));

    find_all('.sortable').forEach(function(elem) {
        elem.addEventListener("click",
                              function(event) {
                                  sort_column(elem);
                              }, false)
    });

};

function sort_table(clicked, key_func) {
    var rows = find_all('.results-table-row');
    var reversed = !clicked.classList.contains('asc');
    var sorted_rows = sort(rows, key_func, reversed);
    /* Whole table is removed here because browsers acts much slower
     * when appending existing elements.
     */
    var thead = document.getElementById("results-table-head");
    document.getElementById('results-table').remove();
    var parent = document.createElement("table");
    parent.id = "results-table";
    parent.appendChild(thead);
    sorted_rows.forEach(function(elem) {
        parent.appendChild(elem);
    });
    document.getElementsByTagName("BODY")[0].appendChild(parent);
}

function sort(items, key_func, reversed) {
    var sort_array = items.map(function(item, i) {
        return [key_func(item), i];
    });

    sort_array.sort(function(a, b) {
        var key_a = a[0];
        var key_b = b[0];

        if (key_a == key_b) return 0;

        if (reversed) {
            return (key_a < key_b ? 1 : -1);
        } else {
            return (key_a > key_b ? 1 : -1);
        }
    });

    return sort_array.map(function(item) {
        var index = item[1];
        return items[index];
    });
}

function key_alpha(col_index) {
    return function(elem) {
        return elem.childNodes[1].childNodes[col_index].firstChild.data.toLowerCase();
    };
}

function key_num(col_index) {
    return function(elem) {
        return parseFloat(elem.childNodes[1].childNodes[col_index].firstChild.data);
    };
}

function key_result(col_index) {
    return function(elem) {
        var strings = ['Error', 'Failed', 'Rerun', 'XFailed', 'XPassed',
                       'Skipped', 'Passed'];
        return strings.indexOf(elem.childNodes[1].childNodes[col_index].firstChild.data);
    };
}

function reset_sort_headers() {
    find_all('.sort-icon').forEach(function(elem) {
        elem.parentNode.removeChild(elem);
    });
    find_all('.sortable').forEach(function(elem) {
        var icon = document.createElement("div");
        icon.className = "sort-icon";
        icon.textContent = "vvv";
        elem.insertBefore(icon, elem.firstChild);
        elem.classList.remove("desc", "active");
        elem.classList.add("asc", "inactive");
    });
}

function toggle_sort_states(elem) {
    //if active, toggle between asc and desc
    if (elem.classList.contains('active')) {
        elem.classList.toggle('asc');
        elem.classList.toggle('desc');
    }

    //if inactive, reset all other functions and add ascending active
    if (elem.classList.contains('inactive')) {
        reset_sort_headers();
        elem.classList.remove('inactive');
        elem.classList.add('active');
    }
}

function is_all_rows_hidden(value) {
  return value.hidden == false;
}

function filter_table(elem) {
    var outcome_att = "data-test-result";
    var outcome = elem.getAttribute(outcome_att);
    class_outcome = outcome + " results-table-row";
    var outcome_rows = document.getElementsByClassName(class_outcome);

    for(var i = 0; i < outcome_rows.length; i++){
        outcome_rows[i].hidden = !elem.checked;
    }

    var rows = find_all('.results-table-row').filter(is_all_rows_hidden);
    var all_rows_hidden = rows.length == 0 ? true : false;
    var not_found_message = document.getElementById("not-found-message");
    not_found_message.hidden = !all_rows_hidden;
}
"""

        session.config.hook.pytest_html_report_title(report=self)

        body = html.body(
            html.script(raw(main_js)),
            html.h1(self.title),
            html.p(
                "Report generated on {} at {} by ".format(
                    generated.strftime("%d-%b-%Y"), generated.strftime("%H:%M:%S")
                ),
                html.a("pytest-html", href=__pypi_url__),
                f" v{__version__}",
            ),
            onLoad="init()",
        )

        body.extend(self._generate_environment(session.config))

        summary_prefix, summary_postfix = [], []
        session.config.hook.pytest_html_results_summary(
            prefix=summary_prefix, summary=summary, postfix=summary_postfix
        )
        body.extend([html.h2("Summary")] + summary_prefix + summary + summary_postfix)

        body.extend(results)

        doc = html.html(head, body)

        unicode_doc = "<!DOCTYPE html>\n{}".format(doc.unicode(indent=2))

        # Fix encoding issues, e.g. with surrogates
        unicode_doc = unicode_doc.encode("utf-8", errors="xmlcharrefreplace")
        return unicode_doc.decode("utf-8")

    def _generate_environment(self, config):
        if not hasattr(config, "_metadata") or config._metadata is None:
            return []

        metadata = config._metadata
        environment = [html.h2("Environment")]
        rows = []

        keys = [k for k in metadata.keys()]
        if not isinstance(metadata, OrderedDict):
            keys.sort()

        for key in keys:
            value = metadata[key]
            if isinstance(value, str) and value.startswith("http"):
                value = html.a(value, href=value, target="_blank")
            elif isinstance(value, (list, tuple, set)):
                value = ", ".join(str(i) for i in sorted(map(str, value)))
            elif isinstance(value, dict):
                sorted_dict = {k: value[k] for k in sorted(value)}
                value = json.dumps(sorted_dict)
            raw_value_string = raw(str(value))
            rows.append(html.tr(html.td(key), html.td(raw_value_string)))

        environment.append(html.table(rows, id="environment"))
        return environment

    def _save_report(self, report_content):
        dir_name = os.path.dirname(self.logfile)
        assets_dir = os.path.join(dir_name, "assets")

        if not os.path.exists(dir_name):
            os.makedirs(dir_name)
        if not self.self_contained and not os.path.exists(assets_dir):
            os.makedirs(assets_dir)

        with open(self.logfile, "w", encoding="utf-8") as f:
            f.write(report_content)
        if not self.self_contained:
            style_path = os.path.join(assets_dir, "style.css")
            with open(style_path, "w", encoding="utf-8") as f:
                f.write(self.style_css)

    def pytest_runtest_logreport(self, report):
        if report.passed:
            self.append_passed(report)
        elif report.failed:
            self.append_failed(report)
        elif report.skipped:
            self.append_skipped(report)
        else:
            self.append_other(report)

    def pytest_collectreport(self, report):
        if report.failed:
            self.append_failed(report)

    def pytest_sessionstart(self, session):
        self.suite_start_time = time.time()

    def pytest_sessionfinish(self, session):
        report_content = self._generate_report(session)
        self._save_report(report_content)

    def pytest_terminal_summary(self, terminalreporter):
        terminalreporter.write_sep("-", f"generated html file: file://{self.logfile}")
