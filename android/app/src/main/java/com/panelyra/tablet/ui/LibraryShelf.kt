package com.panelyra.tablet.ui

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.MenuBook
import androidx.compose.material.icons.filled.Download
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import coil3.compose.AsyncImage
import com.panelyra.tablet.data.ChapterEntity
import com.panelyra.tablet.data.SeriesSummary
import java.io.File

@Composable
fun MacStyleSidebar(selected: String, select: (String) -> Unit) {
    Surface(Modifier.width(192.dp).fillMaxHeight(), color = MaterialTheme.colorScheme.surface) {
        Column(Modifier.padding(horizontal = 14.dp, vertical = 24.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Text("Panelyra", style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.Bold, modifier = Modifier.padding(horizontal = 12.dp))
            Text("Your stories, offline", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.padding(start = 12.dp, bottom = 20.dp))
            for ((id, label, icon) in listOf(Triple("Reader", "Library", Icons.Default.MenuBook), Triple("Downloader", "Downloads", Icons.Default.Download))) {
                Surface(onClick = { select(id) }, modifier = Modifier.fillMaxWidth(), shape = RoundedCornerShape(8.dp),
                    color = if (selected == id) MaterialTheme.colorScheme.primaryContainer else MaterialTheme.colorScheme.surface) {
                    Row(Modifier.padding(14.dp), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                        Icon(icon, null, Modifier.size(21.dp), tint = if (selected == id) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.onSurfaceVariant)
                        Text(label, style = MaterialTheme.typography.titleSmall)
                    }
                }
            }
            Spacer(Modifier.weight(1f))
            Surface(onClick = { select("Settings") }, modifier = Modifier.fillMaxWidth(), shape = RoundedCornerShape(8.dp),
                color = if (selected == "Settings") MaterialTheme.colorScheme.primaryContainer else MaterialTheme.colorScheme.surface) {
                Row(Modifier.padding(14.dp), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                    Icon(Icons.Default.Settings, null, Modifier.size(21.dp)); Text("Settings", style = MaterialTheme.typography.titleSmall)
                }
            }
            Text("Saved books work offline", Modifier.padding(12.dp), style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}

@Composable
private fun LibraryCover(book: SeriesSummary, modifier: Modifier) {
    var failed by remember(book.coverPath) { mutableStateOf(false) }
    val file = book.coverPath?.let(::File)
    if (file?.isFile == true && !failed) {
        AsyncImage(model = file, contentDescription = "${book.title} cover", modifier = modifier.clip(RoundedCornerShape(6.dp)),
            contentScale = ContentScale.Crop, onError = { failed = true })
    } else Surface(modifier.clip(RoundedCornerShape(6.dp)), color = MaterialTheme.colorScheme.primaryContainer) {
        Box(Modifier.padding(12.dp), contentAlignment = Alignment.Center) {
            Text(book.title, style = MaterialTheme.typography.titleMedium, maxLines = 5, overflow = TextOverflow.Ellipsis)
        }
    }
}

@Composable
fun MacStyleSeriesCard(book: SeriesSummary, open: () -> Unit) {
    OutlinedCard(onClick = open, modifier = Modifier.fillMaxWidth(), shape = RoundedCornerShape(12.dp),
        border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant),
        colors = CardDefaults.outlinedCardColors(containerColor = MaterialTheme.colorScheme.surface)) {
        Column(Modifier.padding(10.dp), verticalArrangement = Arrangement.spacedBy(7.dp)) {
            LibraryCover(book, Modifier.fillMaxWidth().aspectRatio(3f / 4f))
            Text(book.title, style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.SemiBold,
                minLines = 2, maxLines = 2, overflow = TextOverflow.Ellipsis)
            Text("${book.chapterCount} saved chapters", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            Text(if (book.unreadCount > 0) "${book.unreadCount} unread" else "All read", style = MaterialTheme.typography.labelSmall,
                color = if (book.unreadCount > 0) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}

@Composable
fun MacStyleContinue(bookTitle: String, chapter: ChapterEntity, open: () -> Unit) {
    OutlinedCard(onClick = open, modifier = Modifier.fillMaxWidth(), shape = RoundedCornerShape(12.dp),
        colors = CardDefaults.outlinedCardColors(containerColor = MaterialTheme.colorScheme.surface),
        border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant)) {
        Column(Modifier.padding(18.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Text("CONTINUE READING", style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.primary)
            Text(bookTitle, style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.SemiBold, maxLines = 2, overflow = TextOverflow.Ellipsis)
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.SpaceBetween) {
                Text("Chapter ${chapter.chapter} · ${(LibraryPresentation.progress(chapter) * 100).toInt()}%", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                Text("Continue reading →", style = MaterialTheme.typography.labelLarge, color = MaterialTheme.colorScheme.primary)
            }
            LinearProgressIndicator(progress = { LibraryPresentation.progress(chapter) }, modifier = Modifier.fillMaxWidth())
        }
    }
}

@Composable
fun MacStyleBookDetails(book: SeriesSummary, chapters: List<ChapterEntity>, tablet: Boolean, back: () -> Unit, open: (ChapterEntity) -> Unit) {
    var query by rememberSaveable(book.id) { mutableStateOf("") }
    var filter by rememberSaveable(book.id) { mutableStateOf("All chapters") }
    var newest by rememberSaveable(book.id) { mutableStateOf(true) }
    val scroll = rememberLazyListState()
    val visible = LibraryPresentation.chapters(chapters, query, filter, newest)
    val start = LibraryPresentation.start(chapters)
    LazyColumn(state = scroll, modifier = Modifier.fillMaxSize(), contentPadding = PaddingValues(if (tablet) 24.dp else 16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp)) {
        item {
            TextButton(onClick = back, modifier = Modifier.heightIn(min = 48.dp)) {
                Icon(Icons.AutoMirrored.Filled.ArrowBack, null); Spacer(Modifier.width(8.dp)); Text("Library")
            }
        }
        item {
            Row(Modifier.fillMaxWidth().padding(bottom = 12.dp), horizontalArrangement = Arrangement.spacedBy(if (tablet) 28.dp else 16.dp)) {
                LibraryCover(book, Modifier.width(if (tablet) 160.dp else 104.dp).aspectRatio(2f / 3f))
                Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                    Text("YOUR LIBRARY", style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.primary)
                    Text(book.title, style = if (tablet) MaterialTheme.typography.headlineMedium else MaterialTheme.typography.titleLarge,
                        fontWeight = FontWeight.Bold)
                    Text("${chapters.size} saved chapters · ${chapters.count { !it.completed }} unread", color = MaterialTheme.colorScheme.onSurfaceVariant, style = MaterialTheme.typography.bodyMedium)
                    Text("Available offline", color = MaterialTheme.colorScheme.primary, style = MaterialTheme.typography.labelMedium)
                    Button(onClick = { start?.let(open) }, enabled = start != null, modifier = Modifier.heightIn(min = 48.dp), shape = RoundedCornerShape(8.dp)) {
                        Text(if (start != null && start.lastReadAt > 0 && !start.completed) "Continue chapter ${start.chapter}" else if (start != null && chapters.any { it.completed }) "Read chapter ${start.chapter}" else "Start reading")
                    }
                }
            }
        }
        item {
            Text("Saved chapters", style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.SemiBold)
            Spacer(Modifier.height(12.dp))
            OutlinedTextField(value = query, onValueChange = { query = it }, placeholder = { Text("Find a chapter") },
                modifier = Modifier.fillMaxWidth(), singleLine = true, shape = RoundedCornerShape(8.dp))
            LazyRow(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
                items(listOf("All chapters", "Unread", "In progress")) { choice ->
                    FilterChip(selected = filter == choice, onClick = { filter = choice }, label = { Text(choice) })
                }
                item { TextButton(onClick = { newest = !newest }, modifier = Modifier.heightIn(min = 48.dp)) { Text(if (newest) "Newest first ↓" else "Oldest first ↑") } }
            }
            Text("${visible.size} of ${chapters.size} chapters", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        if (chapters.isEmpty()) item {
            LinearProgressIndicator(modifier = Modifier.fillMaxWidth())
            Text("Loading saved chapters…")
        } else if (visible.isEmpty()) item { Text("No chapters match. Try another search or filter.", Modifier.padding(vertical = 24.dp)) }
        items(visible, key = { it.chapter }) { chapter ->
            Column {
                Surface(onClick = { open(chapter) }, color = MaterialTheme.colorScheme.background, modifier = Modifier.fillMaxWidth()) {
                    Row(Modifier.fillMaxWidth().heightIn(min = 76.dp), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                        Box(Modifier.width(3.dp).height(44.dp).background(if (chapter.lastReadAt > 0 && !chapter.completed) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.background))
                        Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(5.dp)) {
                            Text("Chapter ${chapter.chapter}", style = MaterialTheme.typography.titleMedium,
                                color = if (chapter.completed) MaterialTheme.colorScheme.onSurfaceVariant else MaterialTheme.colorScheme.onSurface)
                            val status = when { chapter.completed -> "Read"; chapter.lastReadAt > 0 -> "Reading · ${(LibraryPresentation.progress(chapter) * 100).toInt()}%"; else -> "Unread" }
                            Text("$status · ${chapter.pageCount} pages · Saved", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                        Text("Read →", Modifier.padding(end = 8.dp), style = MaterialTheme.typography.labelLarge, color = MaterialTheme.colorScheme.primary)
                    }
                }
                HorizontalDivider(color = MaterialTheme.colorScheme.outlineVariant)
            }
        }
    }
}
