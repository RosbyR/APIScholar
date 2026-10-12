# Creates one OneNote page from page XML via the OneNote desktop app's COM API.
# Called by `paperbot deliver`; prints PAGE_ID=<id> on success.
#
#   powershell -File src\paperbot\deliver\onenote_push.ps1 -Notebook "AI Agents" -Section "Paper Digests" -XmlFile page.xml
#
# The section is created inside the notebook if it does not exist. The notebook must already
# be open in OneNote desktop. OneNote then syncs the page like any other edit.

param(
    [Parameter(Mandatory)] [string]$Notebook,
    [Parameter(Mandatory)] [string]$Section,
    [Parameter(Mandatory)] [string]$XmlFile
)

$ErrorActionPreference = "Stop"
try {
    $on = New-Object -ComObject OneNote.Application

    [ref]$hier = ""
    $on.GetHierarchy("", 2, $hier)            # 2 = hsSections
    [xml]$h = $hier.Value
    $ns = New-Object Xml.XmlNamespaceManager $h.NameTable
    $ns.AddNamespace("one", $h.DocumentElement.NamespaceURI)

    $nb = $h.SelectNodes("//one:Notebook", $ns) | Where-Object { $_.name -eq $Notebook } | Select-Object -First 1
    if (-not $nb) {
        $names = ($h.SelectNodes("//one:Notebook", $ns) | ForEach-Object { $_.name }) -join ", "
        throw "Notebook '$Notebook' is not open in OneNote. Open notebooks: $names"
    }

    $sec = $nb.SelectNodes(".//one:Section", $ns) |
        Where-Object { $_.name -eq $Section -and $_.isInRecycleBin -ne "true" } | Select-Object -First 1
    if ($sec) {
        $sectionId = $sec.ID
    } else {
        [ref]$newId = ""
        $on.OpenHierarchy("$Section.one", $nb.ID, $newId, 3)   # 3 = cftSection: create it
        $sectionId = $newId.Value
    }

    [ref]$pageId = ""
    $on.CreateNewPage($sectionId, $pageId, 0)                   # 0 = npsDefault
    $content = [IO.File]::ReadAllText($XmlFile, [Text.Encoding]::UTF8)
    $content = $content.Replace("__PAGE_ID__", $pageId.Value)
    $on.UpdatePageContent($content)
    try { $on.SyncHierarchy($sectionId) } catch { }             # best effort; OneNote syncs anyway

    Write-Output "PAGE_ID=$($pageId.Value)"
    exit 0
} catch {
    [Console]::Error.WriteLine("onenote_push: $($_.Exception.Message)")
    exit 1
}
