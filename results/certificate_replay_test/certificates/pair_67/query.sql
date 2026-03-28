SELECT
  a.firstname AS FIRSTNAME,
  a.lastname AS LASTNAME,
  b.city AS CITY,
  b.state AS STATE
FROM PERSON AS A
LEFT JOIN ADDRESS AS B
  ON a.personid = b.personid