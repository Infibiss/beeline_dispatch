<?
  include "mysql.php";
  
  $fa = file("rp5.csv");
  for ($i=count($fa)-1; $i>7; $i--) {
    $sa = explode("\";\"", $fa[$i]);
    for ($x=0; $x<count($sa); $x++) $sa[$x] = trim($sa[$x], "\"");
    $sa[23] = $sa[23]*10;
    echo "$sa[0] $sa[1] $sa[23]<br>";
    $da = explode(" ", $sa[0]);
    $da2 = explode(".", $da[0]);
    $da3 = explode(":", $da[1]);
    $q = "UPDATE hours SET temp='$sa[1]'
      WHERE DATE(hour)='$da2[2]-$da2[1]-$da2[0]' AND HOUR(hour)>=$da3[0] AND HOUR(hour)<=$da3[0]+5";
    //echo "$q<br>";
    mysql_query($q);
    echo mysql_error();
    $q = "UPDATE hours SET damp='$sa[23]' 
      WHERE DATE(hour)='$da2[2]-$da2[1]-$da2[0]' AND HOUR(hour)=$da3[0]";
    //echo "$q<br>";
    mysql_query($q);
    echo mysql_error();
    //if ($i>100) break;
  }
?>
