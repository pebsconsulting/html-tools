﻿# linkdiff.py
# By Travis Leithead
# 2016/10/05

from HTMLParser import HTMLParser
import sys
import os.path
import codecs
import json
import urllib
#import time
import math
from multiprocessing import Process, Pipe, Pool, Manager
import re
import multiprocessing
#from operator import itemgetter

# Subclass the parser to build the DOM described below. Since the
# DOM will only be used for tracking links and what they link to, the
# only retained nodes are potential link targets (Element objects)
# and links (LinkElement), as well as all text nodes (TextNode).
# Tree-structure is not important, as I only need to worry about what
# text is "before" and "after" a given target. So the parser (as a depth-
# first traversal of markup tags) will let me build a linear representation
# of the start tags that matter and put the text in the right logical
# order for comparison.
class LinkAndTextHTMLParser(HTMLParser):
    """Parses links and text from HTML"""
    def handle_starttag(self, tag, attrs):
        attrNames = [attr[0] for attr in attrs]
        if tag == "a" and "href" in attrNames:
            attrValues = [attr[1] for attr in attrs]
            # an anchor may also have an id and be a link target as well.
            hasId = ""
            if "id" in attrNames:
                hasId = attrValues[attrNames.index("id")]
            link = LinkElement(self.linkCountIndex, attrValues[attrNames.index("href")], HTMLParser.getpos(self)[0], hasId )
            self.linkCountIndex += 1
            self._append_to_head(link)
            self.doc.links.append(link)
            if hasId != "":
                self._append_to_map(hasId, link)
        elif "id" in attrNames:
            attrValues = [attr[1] for attr in attrs]
            elemId = attrValues[attrNames.index("id")]
            elem = Element(elemId)
            self._append_to_head(elem)
            self._append_to_map(elemId, elem)
        else:
            self.doc.droppedTags += 1

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_data(self, data):
        text = TextNode(data)
        self._append_to_head(text)

    def handle_entityref(self, name):
        self.handle_data("&"+name+";") #pass these through un-modified

    def handle_charref(self, name):
        self.handle_data("&#"+name+";")

    def _append_to_head(self, node):
        if self.head == None:
            self.head = node
            self.doc.start = node
        else: #Hook up the bidirectional links
            self.head.next = node
            node.prev = self.head
            self.head = node

    def _append_to_map(self, key, node):
        if key not in self.doc._idMap:
            self.doc._idMap[key] = node

    def parse(self, markup):
        self.doc = Document()
        self.linkCountIndex = 0
        self.head = None
        self.droppedTagCount = 0
        HTMLParser.reset(self) # among other things, resets the line numbering :-)
        HTMLParser.feed(self, markup)
        HTMLParser.close(self)
        self.head = None
        doc = self.doc
        self.doc = None
        return doc

# Document produced by the Parser has the following IDL

# interface Document {
#   readonly attribute LinkElement[] links;
#   readonly attribute Node start;
#   TreeNode getElementById(str id);
#   readonly attribute unsigned long droppedTags;
# };

# interface Node {
#   readonly attribute Node? prev;
#   readonly attribute Node? next;
# };

# interface TextNode : Node {
#   readonly attribute str textContent;
# };

# only nodes with an ID are retained by the parser.
# interface Element : Node {
#   readonly attribute str id; #reflects the id content attribute
# };

# interface LinkElement : Element {
#   readonly attribute unsigned long index;
#            attribute LinkTreeNodeStatus status;
#   readonly attribute str href;
#            attribute long matchIndex;
#            attribute double matchRatio;
#            attribute double correctRatio;
#   readonly attribute unsigned long lineNo;
# };

# enum LinkTreeNodeStatus = {
#   "non-matched",
#   "matched",
#   "correct",
#   "skipped",
#   "broken",
#   "non-matched-external",
#   "matched-external",
#   "correct-external"
# };

class Document:
    def __init__(self):
        self.links = []
        self.start = None
        self._idMap = {}
        self.droppedTags = 0
        #self.index #added during indexing! hash of "word" <-> [0:count, 1-n:link index]
        #self.unIndexed #added during indexing! list of "words" too common to be useful in indexing.

    def getElementById(self, id):
        if id in self._idMap:
            return self._idMap[id]
        else:
            return None

class Node():
    def __init__(self):
        self.prev = None
        self.next = None

class TextNode(Node):
    def __init__(self, initialText):
        Node.__init__(self)
        self.textContent = initialText
    def __str__(self):
        return "text<"+self.textContent[:40]+ ( "..." if len(self.textContent) > 40 else "" ) + "> (len:"+str(len(self.textContent))+")"

class Element(Node):
    def __init__(self, elemId):
        Node.__init__(self)
        self.id = elemId
        self._cachedContextualText = None
    def __str__(self):
        return '{ "id":"' + self.id.encode('ascii', 'xmlcharrefreplace') + '" }' #because attrs have their entites handled by the parser, and ascii output may not handle them.

class LinkElement(Element):
    def __init__(self, index, href, lineNo, elemId):
        Element.__init__(self, elemId)
        self.index = index
        self.href = href
        self.lineNo = lineNo
        #self.words #added during indexing!
        self.status = "non-matched"
        self.matchIndex = -1
        self.matchRatio = 0.0
        self.correctRatio = 0.0
    def __str__(self):
        return '{"index":' + str(self.index) + ',"matchIndex":' + str(self.matchIndex) + ',"matchRatio":' + str(self.matchRatio)[:5] + ',"correctRatio":' + str(self.correctRatio)[:5] + ',"lineNo":' + str(self.lineNo) + ',"status":"' + self.status + '","href":"' + self.href.encode('ascii', 'xmlcharrefreplace') + '"' + (',"id":"' + self.id + '"' if self.id != '' else '') + '}'
    def __dict__(self):
        return {'index': self.index, 'matchIndex': self.matchIndex, 'matchRatio': self.matchRatio, 'correctRatio': self.correctRatio, 'lineNo': self.lineNo, 'status': self.status, 'href': self.href, 'id': self.id}

def parseTextToDocument(htmlText, statusText):
    parser = LinkAndTextHTMLParser()
    statusUpdate(statusText)
    return parser.parse(htmlText)

# index is a hashtable of "name" <-> [0:count,1-n:list of matching link indexes]
def buildIndex(doc, statusText):
    statusUpdate(statusText)
    doc.index = {}
    doc.unIndexed = [] # because they're too common to be useful...
    tooCommonThreshold = len(doc.links)
    if len(doc.links) > 100:
        tooCommonThreshold = tooCommonThreshold / 3 #if more than 1/3 of all links have this word, then it's too common!
    # slice the text in the document up into words and attach (HALF_WORD_COUNT * 2) number of words to each link
    for linkIndex in xrange(len(doc.links)):
        link = doc.links[linkIndex]
        wordsList = getDirectionalContextualWords(link, True) + getDirectionalContextualWords(link, False)
        link.words = wordsList
        for word in wordsList:
            if word in doc.unIndexed:
                continue
            if word in doc.index:
                doc.index[word][0] += 1 # bump the count
                doc.index[word].append(linkIndex)
                if doc.index[word][0] > tooCommonThreshold:
                    doc.unIndexed.append(word)
                    del doc.index[word] # remove it from the index
            else:
                doc.index[word] = [1, linkIndex]
    #print "Too Common: " + str(len(doc.unIndexed)) + " -- " + str(doc.unIndexed)
    #print "Total unique words: " + str(len(doc.index.keys()))
    #ave = 0
    #for key in doc.index.keys():
    #    ave += doc.index[key][0]
    #print "Average occurances/word: " + str(ave/float(len(doc.index.keys())))

# Process entry point
# For a given list of words, find the matching (set of) index(es) in the provided index.
# Returns an array of candidates that meet the MATCH_RATIO_THRESHOLD bar (>=). Consisting of
# tuples (ratio, associatedIndex, associatedIndex) in preferential order from most preferred (index 0) to
# least preferred.
def StartBuildMatchResult(tuple):
    wordList, otherIndex, otherNonIndexed, otherLinksLen, wordListOriginIndex, renderProgress, mem = tuple
    setGlobals(mem)
    possibleMatches = len(wordList)
    allLinks = [0] * otherLinksLen
    for word in wordList: # typically 20 such words...
        # skip any words from the list that meet the "too common" threshold
        if word in otherNonIndexed:
            possibleMatches -= 1
            continue
        if word in otherIndex:
            linkIndexArray = otherIndex[word] # around 250 on average (could be much smaller or a lot bigger)
            for i in xrange(1, linkIndexArray[0] + 1): # Skip first entry, which is the length
                allLinks[linkIndexArray[i]] += 1 # matching link incremented!
    if possibleMatches == 0:
        return [(0.0, -1, -1)]
    matchValueThreshold = int(math.ceil(possibleMatches * MATCH_RATIO_THRESHOLD))
    possibleMatches = float(possibleMatches) # convert to float so that later division is floating point
    highestMatchValueFound = 0
    bestMatchingIndex = -1
    candidacyAchieved = False
    candidates = [] # Only those that meet the bar
    for i in xrange(otherLinksLen):
        numMatchesOfI = allLinks[i]
        if not candidacyAchieved and numMatchesOfI > highestMatchValueFound:
            highestMatchValueFound = numMatchesOfI
            bestMatchingIndex = i
            if numMatchesOfI >= matchValueThreshold:
                candidates.append((numMatchesOfI/possibleMatches, i, wordListOriginIndex))
                candidacyAchieved = True
        elif candidacyAchieved and numMatchesOfI >= matchValueThreshold:
            candidates.append((numMatchesOfI/possibleMatches, i, wordListOriginIndex))
    if not candidacyAchieved:
        candidates.append((highestMatchValueFound/possibleMatches, bestMatchingIndex, -1))
    # candidates.sort(key=itemgetter(0),reverse=True) # sorts based on 0th item in each tuple (biggest value first)
    if renderProgress:
        progress = mem.progress
        progress += 1
        mem.progress = progress # potentially racy... might loose progress if multiple processes read/write the value while overlapping
        statusUpdateInline("matching... " + str(progress) + "%")
    # Return the list of tuples (ratio, bestMatchingIndex)
    return candidates

def resolveMatchResultConflicts(matchResultsArray, p, mem):
    rowResults = {}
    colResults = {}
    statusUpdate('\nPreparing to resolve matches...')
    matchResultsArrayLen = len(matchResultsArray)
    tenPercent = matchResultsArrayLen / 10 if matchResultsArrayLen > 1000 else matchResultsArrayLen + 1
    percent = 0
    for i in xrange(matchResultsArrayLen):
        if matchResultsArray[i][0][2] == -1:
            matchResultsArray[i] = matchResultsArray[i][0]
        else:
            rowResults[i] = matchResultsArray[i]
            for matchTuple in matchResultsArray[i]:
                if matchTuple[1] not in colResults:
                    colResults[matchTuple[1]] = []
                colResults[matchTuple[1]].append(matchTuple)
        if i % tenPercent + 1 == tenPercent:
            percent += 10
            statusUpdateInline("preparing... " + str(percent) + "%")
    statusUpdate('\nResolving match conflicts...')
    onePercent = matchResultsArrayLen / 100 if matchResultsArrayLen > 1000 else matchResultsArrayLen + 1
    i = 0
    percent = 0
    while i < matchResultsArrayLen:
        if resolveMatchRow(i, rowResults, colResults, matchResultsArray):
            i += 1
            if i % onePercent + 1 == onePercent:
                percent += 1
                statusUpdateInline("resolving... " + str(percent) + "%")

# Returns true if the designted row was resolved; false if some other row was resolved.
def resolveMatchRow(rowIndex, rowDict, colDict, finalMatchArray):
    if not rowIndex in rowDict:
        return True # Resolved in a previous iteration.
    rowLen = len(rowDict[rowIndex])
    assert rowLen != 0, 'If there is a row, it must have more than zero elements...'
    colConstrained = False
    rowConstrained = False
    if rowLen == 1:
        colIndex = rowDict[rowIndex][0][1]
        colLen = len(colDict[colIndex])
        assert len(colDict[colIndex]) > 0, "I don't think this array should ever be empty, if I do maintenance right"
        for tuple in colDict[colIndex]:
            if len(rowDict[tuple[2]]) > 1:
                break
        else:
            colConstrained = True
    for tuple in rowDict[rowIndex]:
        assert len(colDict[tuple[1]]) > 0, "I don't think this array should ever be empty, if I do maintenance right"
        if len(colDict[tuple[1]]) > 1:
            break
    else:
        rowConstrained = True
    if not rowConstrained and not colConstrained:
        return resolveNonConstrainedMatches(rowIndex, rowDict, colDict, finalMatchArray)
    elif rowConstrained and colConstrained:
        finalMatchArray[rowIndex] = rowDict[rowIndex][0]
        del rowDict[rowIndex]
        return True
    elif rowConstrained:
        biggestRatio = rowDict[rowIndex][0][0]
        biggestIndex = 0
        for i in xrange(1, len(rowDict[rowIndex])):
            if rowDict[rowIndex][i][0] > biggestRatio:
                biggestRatio = rowDict[rowIndex][i][0]
                biggestIndex = i

        finalMatchArray[rowIndex] = rowDict[rowIndex][biggestIndex]
        del rowDict[rowIndex]
        return True
    else:
        colIndex = rowDict[rowIndex][0][1]
        biggestRatio = colDict[colIndex][0][0]
        biggestIndex = 0
        for i in xrange(1, len(colDict[colIndex])):
            if colDict[colIndex][i][0] > biggestRatio:
                biggestRatio = colDict[colIndex][i][0]
                biggestIndex = i

        for i in xrange(len(colDict[colIndex])):
            rovingColumnTuple = colDict[colIndex][i]
            rovingRowIndex = rovingColumnTuple[2]
            finalMatchArray[rovingRowIndex] = (rovingColumnTuple[0], rovingColumnTuple[1], rovingColumnTuple[2] if i == biggestIndex else -1)
            del rowDict[rovingRowIndex]
        return rowIndex == biggestIndex

def resolveNonConstrainedMatches(anchorRowIndex, rowDict, colDict, finalMatchArray):
    highestRatio = -0.1
    highestRowDict = highestColDict = None
    rowList = [anchorRowIndex]
    iter = 0
    while iter < len(rowList):
        rowIndex = rowList[iter]
        iter += 1
        for rowIterator in xrange(len(rowDict[rowIndex])):
            colIndex = rowDict[rowIndex][rowIterator][1]
            for tuple in colDict[colIndex]:
                localRowIndex = tuple[2]
                if localRowIndex != rowIndex:
                    if localRowIndex not in rowList:
                        rowList.append(localRowIndex)
                    continue
                if tuple[0] > highestRatio:
                    highestRowDict = {}
                    highestRowDict[localRowIndex] = [tuple]
                    highestColDict = {}
                    highestColDict[colIndex] = [tuple]
                    highestRatio = tuple[0]
                elif tuple[0] == highestRatio:
                    if localRowIndex not in highestRowDict:
                        highestRowDict[localRowIndex] = []
                    if colIndex not in highestColDict:
                        highestColDict[colIndex] = []
                    highestRowDict[localRowIndex].append(tuple)
                    highestColDict[colIndex].append(tuple)
    rowKeys = highestRowDict.keys()
    colKeys = highestColDict.keys()
    if len(rowKeys) == 1 and len(colKeys) == 1:
        selectAndRemoveFromNonConstrainedMatches(rowKeys[0], colKeys[0], rowDict, colDict, finalMatchArray)
        return anchorRowIndex == rowKeys[0]
    for rIndex in rowKeys:
        if len(highestRowDict[rIndex]) == 1:
            selectAndRemoveFromNonConstrainedMatches(rIndex, highestRowDict[rIndex][0][1], rowDict, colDict, finalMatchArray)
            return anchorRowIndex == rIndex
    for cIndex in colKeys:
        if len(highestColDict[cIndex]) == 1:
            rIndex = highestColDict[cIndex][0][2]
            selectAndRemoveFromNonConstrainedMatches(rIndex, cIndex, rowDict, colDict, finalMatchArray)
            return anchorRowIndex == rIndex
    tuple = highestRowDict[rowKeys[0]][0]
    rIndex = tuple[2]
    selectAndRemoveFromNonConstrainedMatches(rIndex, tuple[1], rowDict, colDict, finalMatchArray)
    return anchorRowIndex == rIndex

def selectAndRemoveFromNonConstrainedMatches(rowIndex, colIndex, rowDict, colDict, finalMatchArray):
    selectedRow = rowDict[rowIndex]
    selectedCol = colDict[colIndex]
    for tuple in selectedRow:
        if tuple[1] == colIndex:
            finalMatchArray[rowIndex] = tuple
        else:
            col = colDict[tuple[1]]
            for i in xrange(len(col)):
                if tuple == col[i]:
                    del col[i]
                    break
    for tuple in selectedCol:
        if tuple[2] != rowIndex:
            row = rowDict[tuple[2]]
            for i in xrange(len(row)):
                if tuple == row[i]:
                    if len(row) == 1:
                        finalMatchArray[row[i][2]] = (row[i][0], row[i][1], -1)
                        del rowDict[row[i][2]]
                    else:
                        del row[i]
                    break
    del rowDict[rowIndex]

def applyOwnMatchArray(ownMatchResultsArray, ownLinks):
    assert len(ownMatchResultsArray) == len(ownLinks), 'Baseline and matched lists must have the same length'
    matchesCount = 0
    for rowIndex in xrange(len(ownMatchResultsArray)):
        ratio, otherIndex, ownIndex = ownMatchResultsArray[rowIndex]
        link = ownLinks[rowIndex]
        link.matchRatio = ratio
        link.matchIndex = -1 if ownIndex == -1 else otherIndex
        if ownIndex != -1:
            link.status = 'matched'
            matchesCount += 1
    return matchesCount

def applyOtherMatchArray(otherMatchResultsArray, ownLinks):
    matchesCount = 0
    for tuple in otherMatchResultsArray:
        ratio, ownIndex, otherIndex = tuple
        if ownIndex != -1:
            link = ownLinks[ownIndex]
            link.matchRatio = ratio
            link.matchIndex = otherIndex
            if otherIndex != -1:
                link.status = 'matched'
                matchesCount += 1
    return matchesCount

# If the paramter is true, returns a tuple where
# 0 - skipped total
# 1 - [ (otherIndex, hrefValue) ]
# 2 - [ (otherIndex, [words]) ]
# and "otherIndex" are indexes in the other document's link index (from matchIndex)
def preCheck4Correct(doc, generateOtherLists = False):
    otherExternal = []
    otherWords = []
    skippedTotal = 0
    for link in doc.links:
        if link.status != "matched":
            if check4External(link):
                link.status = "non-matched-external"
            continue
        if link.href in IGNORE_LIST:
            link.status = "skipped"
            skippedTotal += 1
            continue
        if check4External(link):
            link.status = "matched-external"
            # won't know if this is correct until cross-checking with the other process
            if generateOtherLists:
                otherExternal.append((link.matchIndex, link.href))
            continue
        hrefTarget = doc.getElementById(getLinkTarget(link.href))
        if hrefTarget == None:
            link.status = "broken"
            continue
        words = getDirectionalContextualWords(hrefTarget, True) + getDirectionalContextualWords(hrefTarget, False)
        if generateOtherLists:
            otherWords.append((link.matchIndex,words))
        else: #in-place update the word list to be the target's word list!
            link.words = words
    return (skippedTotal, otherExternal, otherWords)

# returns an array of results for each provided array as:
# 0 - total own correct
# 1 - [indexes of potentially correct external links]
# 2 - [(indexOfCorrectLink,correctRatio)]
# where the indexes are per the other document
def check4Correct(doc, otherExternal, otherWords):
    correctExternals = []
    correctWords = []
    for externTuple in otherExternal:
        index, href = externTuple
        link = doc.links[index]
        if link.href == href:
            correctExternals.append(link.matchIndex)
            link.status = 'correct-external'
            link.correctRatio = 1.0
    totalOwnCorrect = len(correctExternals)
    for wordTuple in otherWords:
        index, words = wordTuple
        link = doc.links[index]
        ownWords = link.words
        wordsToOwnWordsRatio = getRatio(words, ownWords)
        # If the lengths of the two lists are the same, then the same ratio of matches is interchangable
        # e.g., for two lists with 4 items, if only 2 items match from one list to the other, more than
        # 2 items cannot match if the lists were interchanged. This is not true when the lists have different
        # lenghts, as the matching ratios can be different...
        if len(words) == len(ownWords):
            link.correctRatio = wordsToOwnWordsRatio
            if wordsToOwnWordsRatio >= MATCH_RATIO_THRESHOLD:
                link.status = 'correct'
                totalOwnCorrect += 1
                correctWords.append((link.matchIndex, wordsToOwnWordsRatio))
            continue
        link.correctRatio = getRatio(ownWords, words)
        if link.correctRatio >= MATCH_RATIO_THRESHOLD:
            link.status = 'correct'
            totalOwnCorrect += 1
            correctWords.append((link.matchIndex, wordsToOwnWordsRatio))
    return (totalOwnCorrect, correctExternals, correctWords)

def getRatio(ownWords, otherWords):
    otherWordsNoDup = [ {'word': w, 'notused': True} for w in otherWords ]
    found = 0
    for word in ownWords:
        for otherWord in otherWordsNoDup:
            if otherWord['notused'] and word == otherWord['word']:
                found += 1
                otherWord['notused'] = False
                break

    return found / float(len(ownWords))

def applyCorrectnessResults(doc, externalCorrectList, wordCorrectList):
    for i in externalCorrectList:
        doc.links[i].status = 'correct-external'
        doc.links[i].correctRatio = 1.0
    for tuple in wordCorrectList:
        link = doc.links[tuple[0]]
        link.status = 'correct'
        link.correctRatio = tuple[1]
    return len(externalCorrectList) + len(wordCorrectList)

HALF_WORD_COUNT = 10
HALF_CONTEXT_MIN = 110 # Tuned using (W3C HTML spec text)

# get HALF_WORD_COUNT words (or less if only less is available) in the indicated direction
def getDirectionalContextualWords(elem, isBeforeText):
    textCount = HALF_CONTEXT_MIN # should be enough, but if not, grow this variable.
    wordCount = 0
    #since lead or tail text may cut off a word (in the middle of a whole word), ask for one more word than needed and drop the potential half-word)
    while wordCount < HALF_WORD_COUNT + 1: # Should loop only once in typical cases...
        text, noMoreTextAvailable = getDirectionalContextualText(elem, isBeforeText, textCount)
        splitArray = re.split('\\W+', text)
        headPos = 0
        tailPos = len(splitArray)
        # discount empty matches at the beginning/end of the array (the nature of 're.split')
        if tailPos > 0 and splitArray[0] == "":
            headPos = 1
        if tailPos > 1 and splitArray[-1] == "":
            tailPos = -1
        splitArray = splitArray[headPos:tailPos]
        if noMoreTextAvailable and len(splitArray) < HALF_WORD_COUNT: # There just isn't any more text; Call it good enough.
            if isBeforeText:
                return [word.lower() for word in splitArray[1:]] #drop the leading word, which is likely cut-off.
            else:
                return [word.lower() for word in splitArray[:-1]] #drop the trailing word, which is likely cut-off.
        wordCount = len(splitArray)
        textCount += 120 # growth factor on retry
    # word count met or exceeded HALF_WORD_COUNT threshold; trim and return
    if isBeforeText: #use list comprehension to lowercase each word in the list.
        return [word.lower() for word in splitArray[-HALF_WORD_COUNT:]] #back HALF_WORD_COUNT from the end, to the end.
    else:
        return [word.lower() for word in splitArray[:HALF_WORD_COUNT]] # 0 to HALF_WORD_COUNT (exclusive)

# Returns a tuple of the requested text and a flag indicating whether more text is available to process.
def getDirectionalContextualText(elem, isBeforeText, characterLimit):
    text = ''
    count = 0
    runner = elem
    while count < characterLimit and runner != None:
        if isinstance(runner, TextNode):
            if isBeforeText:
                text = runner.textContent + text
            else: #after text
                text += runner.textContent
            count += len(runner.textContent)
        runner = runner.prev if isBeforeText else runner.next
    noMoreTextAvailable = (runner == None and count < characterLimit) # not enough characters accumulated!
    if isBeforeText:
        return (text[-characterLimit:], noMoreTextAvailable)
    else:
        return (text[:characterLimit], noMoreTextAvailable)

def check4External(link):
    if link.href[0:1] != '#':
        return True
    return False

def getLinkTarget(href):
    return urllib.unquote(href[1:])

# Validation testing
# =====================================================

def dumpDocument(doc, enumAll=False):
    print "----------------"
    print "Document summary"
    print "----------------"
    print "droppedTags: " + str(doc.droppedTags)
    print "number of links in collection: " + str(len(doc.links))
    print "number of addressable ids: " + str(len(doc._idMap.keys()))
    if enumAll == True:
        print "enumeration of nodes in start:"
    head = doc.start
    counter = 0
    while head != None:
        if enumAll == True:
            print "  " + str(counter) + ") " + str(head)
        head = head.next
        counter += 1
    print "total nodes in document: " + str(counter)

def getAndCompareRatio(elem1, elem2):
    list1 = getDirectionalContextualWords(elem1, True) + getDirectionalContextualWords(elem1, False)
    list2 = getDirectionalContextualWords(elem2, True) + getDirectionalContextualWords(elem2, False)
    return getRatio(list1, list2)

def getContextualText(elem):
    combinedTextBefore, nomore = getDirectionalContextualText(elem, True, 150)
    combinedTextAfter, nomore = getDirectionalContextualText(elem, False, 150)
    return combinedTextBefore + combinedTextAfter

def runTests(mem):
    mem.ignoreList = {'http://test/test/test.com': True}
    mem.progress = 0
    setGlobals(mem)
    # test 1
    parser = LinkAndTextHTMLParser()
    doc = parser.parse("<hello/><there id ='foo' /></there></hello>");
    assert doc.droppedTags == 1, "test1: expected only one dropped tag"
    assert doc.start.id == 'foo', "test1: expected 1st retained element to have id 'foo'"
    assert doc.start.next == None, "test1: element initialized correctly"
    assert doc.getElementById('foo') == doc.start, "test1: document can search for an element by id"
    assert doc.getElementById('foo2') == None, "test1: document fails to retrieve non-existant id"
    assert len(doc.links) == 0, "test1: no links were found"
    #dumpDocument(doc, True)

    # test 2
    doc = parser.parse('<a id="yes" test=one href="http://test.com/file.htm#place">link text sample</a>')
    assert doc.droppedTags == 0, 'test2: no dropped tags'
    assert len(doc.links) == 1, 'test2: link element was placed into the links collection'
    assert doc.links[0] == doc.start, 'test2: the first link is also the start of the document'
    assert doc.start.id == 'yes', 'test2: link elements can also have an id'
    assert doc.start.next.textContent == 'link text sample', 'test2: a text node was properly appended to the start of the document'
    assert doc.start.next.next == None, "test2: only two items in the document's start list"
    assert doc.start.index == 0, "test2: link's index is 0"
    assert doc.start.href == 'http://test.com/file.htm#place', 'test2: retained the URL'
    #dumpDocument(doc, True)

    # test 3
    doc = parser.parse(u'plain text &copy; with &#68; in it<span id="&pound;"></span><a href="#&copy;">text</a>')
    assert doc.droppedTags == 0, 'test3: no dropped tags'
    assert doc.start.next.textContent == '&copy;', 'test3: html entity ref unescaped in text'
    assert doc.getElementById('&pound;') == None, 'test3: html entity ref escaped by parser'
    assert doc.getElementById(u'\xa3') == doc.start.next.next.next.next.next, 'test3: using the unicode value finds the escaped entity ref by id'
    assert doc.start.next.next.next.next.next.next.next.next == None, 'test3: only 8 items in the list'
    assert doc.start.next.next.next.next.next.next.index == 0, 'test3: 2nd-to-last item in the list has 0th index'
    assert doc.start.next.next.next.next.next.next == doc.links[0], 'test3: the link was indexed appropriately'
    assert doc.links[0].href == u'#\xa9', 'test3: href value with entity-ref converted to unicode by the parser'
    #dumpDocument(doc, True)

    # test 4
    doc = parser.parse(u'<div id="&copy;"></div><a href="#&copy;"></a>')
    assert doc.getElementById(doc.links[0].href[1:]) == doc.start, 'test4: unicode attribute handling and escaping is self-consistent for lookups'
    #dumpDocument(doc, True)

    # test 5
    doc = parser.parse('<div id="target">first</div><div id="target">second</div><a href="#target"></a>')
    assert doc.getElementById('target') == doc.start, 'test5: for duplicate ids, link targets (and getElementById) should ignore all but the first ocurence in document order'
    #dumpDocument(doc, True)

    # test 6 - getContextualText
    doc = parser.parse("The <b>freeway</b> can get quite backed-up; that's why I enjoy riding the <div id=target>Connector</div>. It saves me \nlots of time on my commute. Microsoft <i>is quite awesome</i> to provide such a service to their <span class=employee><a>employees</a></span> \nthat live in the <span>Pugot Sound</span> area. Of course, I could get to work a lot faster by driving my car,\nbut then I wouldn't be able to write tests while on the <a href=#target>bus</a>.")
    assert getContextualText(doc.links[0]) == " live in the Pugot Sound area. Of course, I could get to work a lot faster by driving my car,\nbut then I wouldn't be able to write tests while on the bus.", 'test6: text extraction working correctly'
    assert getContextualText(doc.getElementById('target')) == "The freeway can get quite backed-up; that's why I enjoy riding the Connector. It saves me \nlots of time on my commute. Microsoft is quite awesome to provide such a service to their employees \nthat live in the Pugot So", 'test6: target text extraction'

    # test 7 - getAndCompareRatio
    doc = parser.parse("Here's some text that is the same<a href=hi>")
    doc2 = parser.parse("And this sentance won't match up anywhere<a href=bar>")
    assert getAndCompareRatio(doc.links[0], doc.links[0]) == 1.0, 'test7: getAndCompareRatio working for same sentances'
    assert getAndCompareRatio(doc.links[0], doc2.links[0]) < 0.09, 'test7: getAndCompareRatio working for non-similar sentances'
    doc2 = parser.parse("Here's some text that isn't the same<a href=foo>")
    assert getAndCompareRatio(doc.links[0], doc2.links[0]) > 0.85, 'test7: getAndCompareRatio working for similar sentances'

    # test 8 - (new) Validate the complexities of the match resolver
    #     0 1 2 3 4 5 6 7 8
    #   +-------------------
    # 0 | a A
    # 1 |     b
    # 2 |     b                 .. 4 5 6 7 8        .. 4 5 6 7 8         .. 4 5 6 7..
    # 3 |       C               -------------       -------------        -------------
    # 4 |         d d,d d,         d d,  d,            d d,  d,               d,  d,    (row constrained)
    # 5 |         d,d,d d,    =>   d,d,  d,     =>     d,d,  d,      =>     ()
    # 6 |           d D d              ()
    # 7 |         d,  d   d,       d,      d,                  ()
    # 8 |     B

    # A - Row constrained
    # B - Col constrained
    # C - Row + Col constrained
    # D - Unconstrained: pick best match
    # d,- Unconstrained: pick option which is only result in row or column
    # d - Unconstrained: pick top-left option

    array = [
        [(0.7, 0, 0), (0.75, 1, 0)],                            #0
        [(0.7, 2, 1)],                                          #1
        [(0.7, 2, 2)],                                          #2
        [(0.8, 3, 3)],                                          #3
        [(0.7, 4, 4), (0.75, 5, 4), (0.7, 6, 4), (0.75, 7, 4)], #4
        [(0.75, 4, 5),(0.75, 5, 5), (0.7, 6, 5), (0.75, 7, 5)], #5
        [(0.7, 5, 6), (0.8, 6, 6),  (0.7, 7, 6)],               #6
        [(0.75, 4, 7),(0.7, 6, 7),  (0.75, 8, 7)],              #7
        [(0.75, 2, 8)]                                          #8
    ]
    p = Pool(1)
    resolveMatchResultConflicts(array, p, mem)
    p.close()
    assert array[0][0] == 0.75, 'test8: index 0 matches ratio'
    assert array[0][1] == 1, 'test8: index 0 matches other'
    assert array[0][2] == 0, 'test8: index 0 matches index'
    assert array[1][0] == 0.7, 'test8: index 1 matches ratio'
    assert array[1][1] == 2, 'test8: index 1 matches other'
    assert array[1][2] == -1, 'test8: index 1 matches index'
    assert array[2][0] == 0.7, 'test8: index 2 matches ratio'
    assert array[2][1] == 2, 'test8: index 2 matches other'
    assert array[2][2] == -1, 'test8: index 2 matches index'
    assert array[8][0] == 0.75, 'test8: index 8 matches ratio'
    assert array[8][1] == 2, 'test8: index 8 matches other'
    assert array[8][2] == 8, 'test8: index 8 matches index'
    assert array[3][0] == 0.8, 'test8: index 3 matches ratio'
    assert array[3][1] == 3, 'test8: index 3 matches other'
    assert array[3][2] == 3, 'test8: index 3 matches index'
    assert array[6][0] == 0.8, 'test8: index 6 matches ratio'
    assert array[6][1] == 6, 'test8: index 6 matches other'
    assert array[6][2] == 6, 'test8: index 6 matches index'
    assert array[7][0] == 0.75, 'test8: index 7 matches ratio'
    assert array[7][1] == 8, 'test8: index 7 matches other'
    assert array[7][2] == 7, 'test8: index 7 matches index'
    assert array[5][0] == 0.75, 'test8: index 5 matches ratio'
    assert array[5][1] == 4, 'test8: index 5 matches other'
    assert array[5][2] == 5, 'test8: index 5 matches index'
    assert array[4][0] == 0.75, 'test8: index 4 matches ratio'
    assert array[4][1] == 5, 'test8: index 4 matches other'
    assert array[4][2] == 4, 'test8: index 4 matches index'

    # test 9 - put it all together
    markup1 = "This is the beginning link: <a href=#top>Top</a>: when in doubt, use this test <a href=http://test/test/test.com>if</a> you are <a href='http://external/comparing'>comparing lines</a> as sequences of characters, and don't want to <a href=#sync>synch</a> up on blanks or hard <span id='sync'>tabs</span>. The optional arguments a and b are sequences to be compared; both <tt>default</tt> to empty strings. The elements of both sequences must be hashable. The optional argument autojunk can be used to disable the automatic <a href=#not_matched>junk heuristic</a>. New in version 2.7.1: The <a href='http://test/test/test.com'>autojunk</a> parameter.."
    markup2 = "This is the beginning link: <a href=#top>Top</a>: when in doubt, use this test <a href=http://test/test/test.com>if</a> you are <a href='http://external/comparing'>comparing a line</a> as sequences of characters, and don't want to <a href=#sync>synch</a> up on <i>blanks</i> or <b>hard <span id='sync'>tabs</span></b>. The optional arguments a and b are sequences to be compared; both will <tt>default</tt> to empty strings. The elements of both sequences must be hashable--the optional argument autoskip may stop the automatic skipping behavior for the <a href=#not_matched>stop algorithm</a>. With the addition of a new stop algorithm in this document, you may now see that things aren't quite <a href='http://test/test/test.com'>the same</a>.."
    res = diffLinksWithMarkupText(markup1, markup2, mem)
    #dumpJSONResults(res)
    assert len(res.baseAllLinks) == 6, 'test9: parsing validation-- 6 links in markup1'
    assert len(res.srcAllLinks) == 6, 'test9: parsing validation-- 6 links in markup2'
    assert res.baseAllLinks[0].status == 'broken', 'test9: link matching validation: link is broken'
    assert res.baseAllLinks[0].matchIndex == 0, 'test9: link matching validation: matched at 0'
    assert res.srcAllLinks[res.baseAllLinks[0].matchIndex].href == '#top', 'test9: correct link (0) matched in source doc'
    assert res.baseAllLinks[0].matchRatio >= 0.7, 'test9: link matching validation: Ratio is 0.97333-ish'
    assert res.baseAllLinks[0].correctRatio == 0.0, 'test9: link matching validation: default value for correctRatio not-correct'
    assert res.baseAllLinks[1].status == 'skipped', 'test9: link matching validation: link is matched & skipped'
    assert res.baseAllLinks[1].matchIndex == 1, 'test9: link matching validation: matched at 1'
    assert res.srcAllLinks[res.baseAllLinks[1].matchIndex].href == 'http://test/test/test.com', 'test9: correct link (1) matched in source doc'
    assert res.baseAllLinks[1].matchRatio > 0.8, 'test9: link matching validation: Ratio is 0.89.'
    assert res.baseAllLinks[1].correctRatio == 0.0, 'test9: link matching validation: not correct--0 ratio'
    assert res.baseAllLinks[2].status == 'correct-external', 'test9: link matching validation: link is correct, but external'
    assert res.baseAllLinks[2].matchIndex == 2, 'test9: link matching validation: matched at 2'
    assert res.srcAllLinks[res.baseAllLinks[2].matchIndex].href == 'http://external/comparing', 'test9: correct link (2) matched in source doc'
    assert res.baseAllLinks[2].matchRatio > 0.89, 'test9: link matching validation: Ratio is 0.9-ish'
    assert res.baseAllLinks[2].correctRatio == 1.0, 'test9: link matching validation: external link is 100% correct/same'
    assert res.baseAllLinks[3].status == 'correct', 'test9: link matching validation: link is correct' + ' got: ' + res.baseAllLinks[3].status + ' ratio: ' + str(res.baseAllLinks[3].correctRatio)
    assert res.baseAllLinks[3].matchIndex == 3, 'test9: link matching validation: matched at 3'
    assert res.srcAllLinks[res.baseAllLinks[3].matchIndex].href == '#sync', 'test9: correct link (2) matched in source doc'
    assert res.baseAllLinks[3].matchRatio > 0.88, 'test9: link matching validation: Ratio is approx 0.8815'
    assert res.baseAllLinks[3].correctRatio > 0.89, 'test9: link matching validation: Correctness matching ratio is approx 0.894'
    assert res.baseAllLinks[4].status == 'non-matched', 'test9: link matching validation: link is not matched'
    assert res.baseAllLinks[4].matchIndex == -1, 'test9: link matching validation: failed to match--all broken links checked, index set -1 to avoid confusion'
    assert res.baseAllLinks[4].matchRatio < 0.36, 'test9: link matching validation: Match ratio is too low to match, approx 0.359'
    assert res.baseAllLinks[4].correctRatio == 0.0, 'test9: link matching validation: un-matched correctRatio is 0.0'
    assert res.baseAllLinks[5].status == 'non-matched-external', 'test9: link matching validation: link is not matched and external'
    assert res.baseAllLinks[5].matchIndex == -1, 'test9: link matching validation: set to -1'
    assert res.baseAllLinks[5].matchRatio < 0.47, 'test9: link matching validation: not matched, low ratio (0.465)'
    assert res.baseAllLinks[5].correctRatio == 0.0, 'test9: link matching validation: Correctness n/a (0.0)'

    # test 10 - check 'matched' and 'matched-external'
    markup1 =  "<span id=matched>One</span> of the common, difficult to figure-out problems in the current HTML spec is\n"
    markup1 += "whether links are 'correct'. Not 'correct' as in syntax or as opposite to broken\n"
    markup1 += "links, but rather that the link in question goes to the semantically correct place\n"
    markup1 += "in the spec or other linked <a href='http://external/place1'>spec</a>. "
    markup1 += "<a href=#matched>Correctness</a>, in this sense, can only be determined\n"
    markup1 += "by comparing the links to a canonical 'correct' source. In the case of the W3C HTML\n"
    markup1 += "spec, the source used for determining correctness is the WHATWG version of the spec."

    markup2 =  "One of the common, difficult to figure-out problems in the current HTML spec is\n"
    markup2 += "whether links are 'correct'. Not 'correct' as in syntax or as opposite to broken\n"
    markup2 += "links, but rather that the link in question goes to the semantically correct place\n"
    markup2 += "in the spec or other linked <a href='http://external/place2'>spec</a>. "
    markup2 += "<a href=#matched>Correctness</a>, in this sense, can only be determined\n"
    markup2 += "by comparing the links to a canonical 'correct' source. In the case of the W3C HTML\n"
    markup2 += 'spec, the source used for determining correctness is the WHATWG <span id=matched>version</span> of the spec.'
    res = diffLinksWithMarkupText(markup1, markup2, mem)
    #dumpJSONResults(res)
    assert len(res.baseAllLinks) == 2, 'test10: parsing validation-- 2 links in markup1'
    assert len(res.srcAllLinks) == 2, 'test10: parsing validation-- 2 links in markup2'
    assert res.baseAllLinks[0].status == 'matched-external', 'test10: link matching validation: link is matched, but external (and not correct)'
    assert res.baseAllLinks[0].matchIndex == 0, 'test10: link matching validation: matched at 0'
    assert res.srcAllLinks[res.baseAllLinks[0].matchIndex].href == 'http://external/place2', 'test10: correct index (0) matched in source doc'
    assert res.baseAllLinks[0].matchRatio > 0.99, 'test10: link matching validation: Ratio is 1.0'
    assert res.baseAllLinks[0].correctRatio == 0.0, 'test10: link matching validation: default value for not-correct'
    assert res.baseAllLinks[0].lineNo == 4, 'test10: line number is correct (4)'
    assert res.baseAllLinks[1].status == 'matched', 'test10: link matching validation: link is matched'
    assert res.baseAllLinks[1].matchIndex == 1, 'test10: link matching validation: matched at 1'
    assert res.srcAllLinks[res.baseAllLinks[1].matchIndex].href == '#matched', 'test10: correct link (1) matched in source doc'
    assert res.baseAllLinks[1].matchRatio > 0.99, 'test10: link matching validation: Ratio is 1.0'
    assert res.baseAllLinks[1].correctRatio < 0.3, 'test10: link matching validation: not correct--0.293 ratio'

    # test 11 - href's with percent-encoding... (one-way, works for hrefs, not for targets)
    # note, Chrome 53 stable: tries to match link targets using both the pre-decoded text as well as the post-decoded text...Firefox/Edge do not do this, so this tool will not either.
    markup1 = '<p id="first()">first target</p><a href="#last()">goto last</a><a href="#last%28%29">alternate last</a>. This is some content. And here is some links: <a href="#first%28%29">goto first</a><p id="last%28%29">last target</p>'
    res = diffLinksWithMarkupText(markup1, markup1, mem)
    #dumpJSONResults(res)
    assert res.baseAllLinks[0].href == '#last()', 'test11: no fancy escaping done to these characters by the HTMLParser implementation.'
    assert res.baseAllLinks[0].status == 'broken', 'test11: percent-encoded attribute values in id are not converted to match.'
    assert res.baseAllLinks[1].href == '#last%28%29', 'test11: no fancy escaping done to percent-encoded characters by the HTMLParser implementation.'
    assert res.baseAllLinks[1].status == 'broken', 'test11: href values are always decoded before checking for literal matching ids (see note on Chrome above)'
    assert res.baseAllLinks[2].status == 'correct', 'test11: percent-encoded attribute values in hrefs are decoded to match.'

    # test 12 - new indexing technique
    markup1 =  "<span id=matched>One</span> of the common, difficult to figure-out problems in the \n"
    markup1 += "current HTML spec is whether links are 'correct'. Not 'correct' as in syntax or as \n"
    #                                                                                  -10      -9
    markup1 += "opposite to broken links, but rather that the link in question goes to the semantically\n"
    #             -8      -7  -6  -5  -4  -3   -2    -1                                     1
    markup1 += "correct place in the Spec or other linked <a href='http://external/place1'>spec</a>. \n"
    #                2        3   4    5     6    7   8     9      10
    markup1 += "Correctness, in this sense, can only be determined by comparing \n"
    markup1 += "the links to a canonical 'correct' source. In the case of the W3C HTML spec, the \n"
    markup1 += "source used for determining correctness is the WHATWG version of the spec."
    doc = parseTextToDocument(markup1, "")
    #dumpDocument(doc, True)
    resultWordList = getDirectionalContextualWords(doc.links[0], True)
    assert len(resultWordList) == HALF_WORD_COUNT, "test12: getDirectionalContextualWords returns "+str(HALF_WORD_COUNT)+" items from front of link"
    testList = ['the', 'semantically', 'correct', 'place', 'in', 'the', 'spec', 'or', 'other', 'linked']
    for i in xrange(len(testList)):
        assert testList[i] == resultWordList[i], "test12: validating expected words before link"
    resultWordList = getDirectionalContextualWords(doc.links[0], False)
    assert len(resultWordList) == HALF_WORD_COUNT, "test12: getDirectionalContextualWords returns "+str(HALF_WORD_COUNT)+" items from back of link"
    testList = ['spec', 'correctness', 'in', 'this', 'sense', 'can', 'only', 'be', 'determined', 'by']
    for i in xrange(len(testList)):
        assert testList[i] == resultWordList[i], "test12: validating expected words after link"
    buildIndex(doc, "")
    assert len(doc.index.keys()) == 14, "test12: total number of unique words indexed is 14 (others were in the too common list"
    testList = ['correct', 'be', 'linked', 'correctness', 'by', 'this', 'only', 'other', 'place', 'can', 'sense', 'semantically', 'determined', 'or']
    for word in doc.index.keys():
        assert word in testList, "test12: only expected words are in the index"
        assert doc.index[word][0] == 1, "test12: indexed words all have an initial occurance count of 1"
        assert doc.index[word][1] == 0, "test12: words are all found in first link"
        del testList[testList.index(word)]
    print 'All tests passed'

# Input processing
# =====================================================

def cmdhelp():
    print "linkdiff - A diffing tool for HTML hyperlink semantic validation"
    print ""
    print "  The tool compares the hyperlinks (anchor tags with an href attribute) in a baseline"
    print "  document with those in a source document. It checks that both documents have the same"
    print "  set of hyperlinks, and that those hyperlinks link to the same relative places within"
    print "  their respective documents. The output is a JSON structure of the diff results."
    print ""
    print "Usage:"
    print ""
    print "  linkdiff [flags] <baseline html file> <source html file>"
    print ""
    print "    The baseline and source files may be paths to the respective files on disk, or URLs."
    print "    The only supported protocols for URLs are 'http' and 'https'; any other protocol will"
    print "    be interpreted as a local file path."
    print ""
    print "Flags:"
    print ""
    print "  -ratio <value between 0 and 1>"
    print ""
    print "    Example: linkdiff -ratio 0.9 baseline_doc.html source_doc.html"
    print ""
    print "      Overrides the default ratio used for verifying that a link is in the same place in"
    print "      both specs, and that the hyperlink's targets are in the same relative place. A low"
    print "      ratio (e.g., 0.25 or 25%) is more permissive in that only 25% of the relative surrounding"
    print "      content must be the same to be considered a match. A higher ratio (e.g., 0.9 or 90%) is"
    print "      more strict. The default (if the flag is not supplied) is 0.7 or 70%."
    print ""
    print "  -ignorelist <ignorelist_file>"
    print ""
    print "    Example: linkdiff -ignorelist ignore_list.json baseline_doc.html source_doc.html"
    print ""
    print "      The ignore list is a file containing a JSON object with a single property named"
    print "      'ignoreList' whose value is an array of strings. The strings should contain the absolute"
    print "      or relative URLs to skip/ignore during link verification. String matching is used to"
    print "      apply the strings to href values, so exact matches are required. The ignore list applies"
    print "      to both baseline and source html files"
    print ""
    print "  -statsonly"
    print ""
    print "    Example: linkdiff -statsonly http://location/of/baseline ../source/doc/location.htm"
    print ""
    print "      The JSON output is limited to the statistical values from the processing results. The"
    print "      detailed link report for both baseline and source documents is omitted."
    print ""
    print "  -v"
    print ""
    print "    Example: linkdiff -v baseline_doc.html source_doc.html"
    print ""
    print "      Shows verbose status messages during processing. Useful for monitoring the progress"
    print "      of the tool."
    print ""
    print "  -parallelmatch <value greater than 1>"
    print ""
    print "    Example: linkdiff -parallelmatch 1 baseline.html http://source.html"
    print "    Example: linkdiff -parallelmatch 16 baseline.html http://source.html"
    print ""
    print "      The first examples disables any multi-processing during matching. Only one process"
    print "      is used to perform link matching (multiple processes will be used to load/parse both"
    print "      documents). The second example overrides the default (number of cores avilable on the"
    print "      OS) to use 16 processes for link matching."
    print ""
    print "  -runtests"
    print ""
    print "    Example: linkdiff -runtests"
    print ""
    print "      When this flag is used, the <baseline html file> and <source html file> input values"
    print "      are not required. This flag runs the self-tests to ensure the code is working as expected"
    print ""
SHOW_STATUS = None
SHOW_ALL_STATUS = None
MATCH_RATIO_THRESHOLD = None
IGNORE_LIST = None
CPU_COUNT = None
PROCESS_ERROR = None

def getSharedMemory():
    processManager = Manager()
    return processManager.Namespace()

def setGlobals(mem):
    global CPU_COUNT
    global IGNORE_LIST
    global MATCH_RATIO_THRESHOLD
    global SHOW_ALL_STATUS
    global PROCESS_ERROR
    global SHOW_STATUS
    SHOW_STATUS = mem.showStatus
    SHOW_ALL_STATUS = mem.showAllStats
    MATCH_RATIO_THRESHOLD = mem.ratio
    PROCESS_ERROR = mem.error
    IGNORE_LIST = mem.ignoreList
    CPU_COUNT = mem.cpuCount

def diffLinksWithFilename(baselineFilename, srcFilename, mem):
    forBaseline, forSource = Pipe()
    p = Process(target=StartBaselineProcessorWithFileName, args=(baselineFilename, mem, forBaseline), name='Proc_baseline_w_filename')
    p.start()
    output = StartSourceWithFilename(srcFilename, mem, forSource)
    p.join()
    return output

def diffLinksWithMarkupText(baselineText, sourceText, mem):
    forBaseline, forSource = Pipe()
    p = Process(target=StartBaselineProcessorWithMarkupText, args=(baselineText, mem, forBaseline), name='Proc_baseline_w_text')
    p.start()
    output = StartSourceWithMarkupText(sourceText, mem, forSource)
    p.join()
    return output

# Process entry points
## ----------------------------

def StartBaselineProcessorWithFileName(baseLineFilenameToLoad, mem, comm):
    setGlobals(mem)
    baseDocText = loadDocumentText(baseLineFilenameToLoad)
    if baseDocText == None:
        mem.error = True
        return
    StartBaselineProcessorWithMarkupText(baseDocText, mem, comm)

def StartBaselineProcessorWithMarkupText(text, mem, comm):
    setGlobals(mem)
    baselineDoc = parseTextToDocument(text, 'Parsing baseline document...')
    buildIndex(baselineDoc, 'Indexing baseline document...')
    assert comm.recv() == 'start:baseline matching', 'Expected start:baseline matching signal from other process...'
    statusUpdate('Matching baseline document links to source document...(this may take a few minutes)')
    srcIndex = mem.srcIndex
    srcNonIndexed = mem.srcUnIndexed
    srcLinksLen = mem.srcLinksLen
    mem.progress = 0
    p = Pool(CPU_COUNT)
    inputParamsArray = []
    onePercent = len(baselineDoc.links) / 100 if len(baselineDoc.links) > 1000 else len(baselineDoc.links) + 1
    for ownIndex in xrange(len(baselineDoc.links)):
        inputParamsArray.append((baselineDoc.links[ownIndex].words, srcIndex, srcNonIndexed, srcLinksLen, ownIndex, ownIndex % onePercent + 1 == onePercent, mem))
    baselineMatches = p.map(StartBuildMatchResult, inputParamsArray)
    resolveMatchResultConflicts(baselineMatches, p, mem)
    p.close()
    mem.baselineMatches = baselineMatches
    mem.ownLinksLen = len(baselineDoc.links)
    comm.send('apply:baseline matches')
    mem.totalMatchCount = applyOwnMatchArray(baselineMatches, baselineDoc.links)
    mem.baseSkippedCount, mem.checkExternals, mem.checkWords = preCheck4Correct(baselineDoc, True)
    comm.send('start:correctness check')
    assert comm.recv() == 'apply:correctness results', 'Expected apply:correctness results signal from other process...'
    mem.totalCorrectCount = applyCorrectnessResults(baselineDoc, mem.externalCorrectResults, mem.wordCorrectResults)
    mem.baseAllLinks = baselineDoc.links
    comm.send('done')

def StartSourceWithFilename(sourceFilename, mem, comm):
    setGlobals(mem)
    sourceDocText = loadDocumentText(sourceFilename)
    if sourceDocText == None or mem.error:
        return
    return StartSourceWithMarkupText(sourceDocText, mem, comm)

def StartSourceWithMarkupText(text, mem, comm):
    setGlobals(mem)
    sourceDoc = parseTextToDocument(text, 'Parsing source document...')
    buildIndex(sourceDoc, 'Indexing source document...')
    if mem.error:
        return None
    mem.srcIndex = sourceDoc.index
    mem.srcUnIndexed = sourceDoc.unIndexed
    mem.srcLinksLen = len(sourceDoc.links)
    comm.send('start:baseline matching')
    assert comm.recv() == 'apply:baseline matches', 'Expected apply:baseline matches signal from other process...'
    totalMatchCount = applyOtherMatchArray(mem.baselineMatches, sourceDoc.links)
    srcSkippedTotal = preCheck4Correct(sourceDoc)[0]
    assert comm.recv() == 'start:correctness check', 'Expected start:correctness check signal from other process...'
    srcCorrectTotal, mem.externalCorrectResults, mem.wordCorrectResults = check4Correct(sourceDoc, mem.checkExternals, mem.checkWords)
    comm.send('apply:correctness results')
    resultOb = lambda : None
    resultOb.srcAllLinks = sourceDoc.links
    assert comm.recv() == 'done', 'Expected done signal from other process...'
    assert totalMatchCount == mem.totalMatchCount, 'Total matches should be identical between both processes'
    resultOb.statTotalMatches = mem.totalMatchCount
    resultOb.statPotentialMatches = min(mem.ownLinksLen, len(sourceDoc.links)) - max(mem.baseSkippedCount, srcSkippedTotal)
    resultOb.statTotalCorrect = min(mem.totalCorrectCount, srcCorrectTotal)
    resultOb.baseAllLinks = mem.baseAllLinks
    return resultOb

def getFlagValue(flag):
    index = sys.argv.index(flag)
    if index + 1 < len(sys.argv)-2: # [0] linkdiff [len-2] baseline_doc [len-1] src_doc
        return sys.argv[index+1]
    else:
        return None

def setRatio(newRatio, mem):
    if newRatio == None:
        return
    newRatio = float(newRatio)
    # clamp from 0..1
    newRatio = max(min(newRatio, 1.0), 0.0)
    mem.ratio = newRatio
    statusUpdate('Using custom ratio: ' + str(newRatio))

def setProcesses(matchProcesses, mem):
    if matchProcesses == None:
        return
    matchProcesses = max(int(matchProcesses, 10), 1)
    mem.cpuCount = matchProcesses
    statusUpdate('Will use ' + str(matchProcesses) + ' processes for matching')

def setIgnoreList(newListFile, mem):
    localIgnoreList = {}
    if newListFile == None:
        return
    ignoreRoot = json.loads(getTextFromLocalFile(newListFile))
    if not "ignoreList" in ignoreRoot:
        print "Ignore list format error: expected an root object with key 'ignoreList'"
        return
    listOIgnoreVals = ignoreRoot["ignoreList"]
    if not isinstance(listOIgnoreVals, list): #check for built-in list type.
        print "Ignore list format error: expected the 'ignoreList' key to have a list as its value"
        return
    counter = 0
    for ignoreItem in listOIgnoreVals:
        if isinstance(ignoreItem, basestring):
            localIgnoreList[ignoreItem] = True
            counter += 1
    mem.ignoreList = localIgnoreList
    statusUpdate('Using ignore list; entries found: ' + str(counter))

def processCmdParams():
    mem = getSharedMemory()
    mem.showStatus = False
    mem.showAllStats = True
    mem.ratio = 0.7
    mem.error = False
    mem.cpuCount = multiprocessing.cpu_count()
    mem.ignoreList = {}
    if len(sys.argv) == 1:
        return cmdhelp()
    if '-runtests' in sys.argv:
        return runTests(mem)
    expectedArgs = 3
    if not isPython64bit():
        print "********"
        print "WARNING: "
        print "********"
        print "MemoryErrors may occur during link matching on large documents."
        print "A 64-bit Python interpreter is recommended to run this program."
        print ""
    if '-v' in sys.argv:
        mem.showStatus = True
        setGlobals(mem)
        expectedArgs += 1
    if '-statsonly' in sys.argv:
        mem.showAllStats = False
        expectedArgs += 1
    if '-ratio' in sys.argv:
        setRatio(getFlagValue('-ratio'), mem)
        expectedArgs += 2
    if '-parallelmatch' in sys.argv:
        setProcesses(getFlagValue('-parallelmatch'), mem)
        expectedArgs += 2
    if '-ignorelist' in sys.argv:
        setIgnoreList(getFlagValue('-ignorelist'), mem)
        expectedArgs += 2
    if len(sys.argv) < expectedArgs:
        print 'Usage error: <baseline_doc.html> and <source_doc.html> parameters required.'
        return
    outStruct = diffLinksWithFilename(sys.argv[expectedArgs - 2], sys.argv[expectedArgs - 1], mem)
    if outStruct != None:
        dumpJSONResults(outStruct)

def dumpJSONResults(ob):
    statusUpdate('JSON output:')
    statusUpdate('')
    print '{'
    print '  "ratioThreshold": ' + str(MATCH_RATIO_THRESHOLD) + ','
    print '  "matchingLinksTotal": ' + str(ob.statTotalMatches) + ','
    print '  "correctLinksTotal": ' + str(ob.statTotalCorrect) + ','
    print '  "potentialMatchingLinksSetSize": ' + str(ob.statPotentialMatches) + ','
    print '  "percentMatched": ' + str(float(ob.statTotalMatches) / float(ob.statPotentialMatches))[:5] + ','
    print '  "percentCorrect": ' + str(float(ob.statTotalCorrect) / float(ob.statPotentialMatches))[:5] + ','
    dumpJSONDocResults(ob.srcAllLinks, 'sourceDoc', ob.statTotalMatches, True)
    dumpJSONDocResults(ob.baseAllLinks, 'baselineDoc', ob.statTotalMatches, False)
    print '}'

def dumpJSONDocResults(links, docName, numMatchingLinks, addTrailingComma):
    print '  "' + docName + '": {'
    linkTotal = len(links)
    print '    "linksTotal": ' + str(linkTotal) + ','
    print '    "nonMatchedTotal": ' + str(linkTotal - numMatchingLinks) + (',' if SHOW_ALL_STATUS else '')
    if SHOW_ALL_STATUS:
        print '    "linkIndex": [ '
        for link in links:
            print '      ' + str(link) + (',' if link.index < linkTotal - 1 else '')

        print '    ]'
    print '  }' + (',' if addTrailingComma else '')

def statusUpdate(text):
    if SHOW_STATUS:
        print text

def statusUpdateInline(text):
    if SHOW_STATUS:
        print text + "\r",

def loadDocumentText(urlOrPath):
    if urlOrPath[0:1] == '"':
        urlOrPath = urlOrPath[1:-1]
    if urlOrPath[:7] == "http://" or urlOrPath[:8] == "https://":
        return loadURL(urlOrPath)
    else: #assume file path...
        return getTextFromLocalFile(urlOrPath) # may return None

def getTextFromLocalFile(fileString):
    if fileString[0:1] == '"':
        fileString = fileString[1:-1]
    normalizedfileString = os.path.abspath(fileString)
    if not os.path.isfile(normalizedfileString):
        print "File not found: '" + fileString + "' is not a file (or was not found)"
        return
    with open(fileString, 'r') as file:
        return toUnicode(file.read())

def loadURL(url):
    try:
        if SHOW_STATUS:
            print "Getting '" + url + "' from the network..."
        urlhandle = urllib.urlopen(url)
        contents = toUnicode(urlhandle.read())
        urlhandle.close()
        return contents
    except IOError:
        print 'Error opening network location: ' + url
        return None

def toUnicode(raw):
    if raw.startswith(codecs.BOM_UTF16_LE) or raw.startswith(codecs.BOM_UTF16_BE):
        return raw.decode("utf-16", "replace")
    elif raw.startswith(codecs.BOM_UTF8):
        return raw.decode("utf-8-sig", "replace") #decoding errors substitute the replacement character
    else:
        return raw.decode("utf-8", "replace") # assume it.

def isPython64bit():
    return struct.calcsize("P") == 8

# Only the main process should execute this (spawned processes will skip it)
if __name__ == '__main__':
    processCmdParams()