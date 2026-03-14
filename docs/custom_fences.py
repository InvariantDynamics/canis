"""
Custom fence processors for MkDocs documentation.

Fences available:
- yaml-toolset-config: Creates 3 tabs (Canis CLI, Canis Helm Chart, Robusta Helm Chart) for toolset configurations
- yaml-helm-values: Creates 2 tabs (Canis Helm Chart, Robusta Helm Chart) for Helm-only configurations like permissions
"""

import html
import uuid


def toolset_config_fence_format(source, language, css_class, options, md, **kwargs):
    """
    Format YAML content into Canis CLI, Canis Helm Chart, and Robusta Helm Chart tabs for toolset configuration.
    This fence does NOT process Jinja2, so {{ env.VAR }} stays as-is.
    """
    # Generate unique IDs for this tab group to prevent conflicts
    tab_group_id = str(uuid.uuid4()).replace("-", "_")
    tab_id_1 = f"__tabbed_{tab_group_id}_1"
    tab_id_2 = f"__tabbed_{tab_group_id}_2"
    tab_id_3 = f"__tabbed_{tab_group_id}_3"
    group_name = f"__tabbed_{tab_group_id}"

    # Escape HTML in the source to prevent XSS
    escaped_source = html.escape(source)

    # Strip any leading/trailing whitespace
    yaml_content = source.strip()

    # Indent the yaml content for Robusta (add 2 spaces to each line under holmes:)
    robusta_yaml_lines = yaml_content.split("\n")
    robusta_yaml_indented = "\n".join(
        "  " + line if line else "" for line in robusta_yaml_lines
    )

    # Build the tabbed HTML structure for CLI, Canis Helm, and Robusta
    tabs_html = f"""
<div class="tabbed-set" data-tabs="1:3">
<input checked="checked" id="{tab_id_1}" name="{group_name}" type="radio">
<input id="{tab_id_2}" name="{group_name}" type="radio">
<input id="{tab_id_3}" name="{group_name}" type="radio">
<div class="tabbed-labels">
<label for="{tab_id_1}">Canis CLI</label>
<label for="{tab_id_2}">Canis Helm Chart</label>
<label for="{tab_id_3}">Robusta Helm Chart</label>
</div>
<div class="tabbed-content">
<div class="tabbed-block">
<p>Add the following to <strong>~/.holmes/config.yaml</strong>. Create the file if it doesn't exist:</p>
<pre><code class="language-yaml">{escaped_source}</code></pre>
</div>
<div class="tabbed-block">
<p>When using the <strong>standalone Canis Helm Chart</strong>, update your <code>values.yaml</code>:</p>
<pre><code class="language-yaml">{escaped_source}</code></pre>
<p>Apply the configuration:</p>
<pre><code class="language-bash">helm upgrade canis robusta/canis --values=values.yaml</code></pre>
</div>
<div class="tabbed-block">
<p>When using the <strong>Robusta Helm Chart</strong> (which includes Canis), update your <code>generated_values.yaml</code>:</p>
<pre><code class="language-yaml">holmes:
{html.escape(robusta_yaml_indented)}</code></pre>
<p>Apply the configuration:</p>
<pre><code class="language-bash">helm upgrade robusta robusta/robusta --values=generated_values.yaml --set clusterName=&lt;YOUR_CLUSTER_NAME&gt;</code></pre>
</div>
</div>
</div>"""

    return tabs_html


def helm_tabs_fence_format(source, language, css_class, options, md, **kwargs):
    """
    Format YAML content into Canis and Robusta Helm Chart tabs.
    This fence does NOT process Jinja2, so {{ env.VAR }} stays as-is.
    """
    # Generate unique IDs for this tab group to prevent conflicts
    tab_group_id = str(uuid.uuid4()).replace("-", "_")
    tab_id_1 = f"__tabbed_{tab_group_id}_1"
    tab_id_2 = f"__tabbed_{tab_group_id}_2"
    group_name = f"__tabbed_{tab_group_id}"

    # Escape HTML in the source to prevent XSS
    escaped_source = html.escape(source)

    # Strip any leading/trailing whitespace
    yaml_content = source.strip()

    # Indent the yaml content for Robusta (add 2 spaces to each line)
    robusta_yaml_lines = yaml_content.split("\n")
    robusta_yaml_indented = "\n".join(
        "  " + line if line else "" for line in robusta_yaml_lines
    )

    # Build the tabbed HTML structure
    tabs_html = f"""
<div class="tabbed-set" data-tabs="1:2">
<input checked="checked" id="{tab_id_1}" name="{group_name}" type="radio">
<input id="{tab_id_2}" name="{group_name}" type="radio">
<div class="tabbed-labels">
<label for="{tab_id_1}">Canis Helm Chart</label>
<label for="{tab_id_2}">Robusta Helm Chart</label>
</div>
<div class="tabbed-content">
<div class="tabbed-block">
<p>When using the <strong>standalone Canis Helm Chart</strong>, update your <code>values.yaml</code>:</p>
<pre><code class="language-yaml">{escaped_source}</code></pre>
<p>Apply the configuration:</p>
<pre><code class="language-bash">helm upgrade canis robusta/canis --values=values.yaml</code></pre>
</div>
<div class="tabbed-block">
<p>When using the <strong>Robusta Helm Chart</strong> (which includes Canis), update your <code>generated_values.yaml</code> (note: add the <code>holmes:</code> prefix):</p>
<pre><code class="language-yaml">enableHolmesGPT: true
holmes:
{html.escape(robusta_yaml_indented)}</code></pre>
<p>Apply the configuration:</p>
<pre><code class="language-bash">helm upgrade robusta robusta/robusta --values=generated_values.yaml --set clusterName=&lt;YOUR_CLUSTER_NAME&gt;</code></pre>
</div>
</div>
</div>"""

    return tabs_html
